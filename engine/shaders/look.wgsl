// NV12 in, look applied, NV12 out — one compute pass, no textures.
//
// Each invocation owns a 4x2 pixel block, which is the smallest region that
// lines up with both NV12's half-resolution chroma and 4-byte storage writes.
// That makes every read and write exactly one aligned u32, so no two
// invocations ever touch the same word and no atomics are needed.

struct Params {
    width: u32,
    height: u32,
    look: u32,
    strength: f32,
};

@group(0) @binding(0) var<uniform> params: Params;
@group(0) @binding(1) var<storage, read> src: array<u32>;
@group(0) @binding(2) var<storage, read_write> dst: array<u32>;

fn unpack4(word: u32) -> vec4<f32> {
    return vec4<f32>(
        f32( word         & 0xffu),
        f32((word >>  8u) & 0xffu),
        f32((word >> 16u) & 0xffu),
        f32((word >> 24u) & 0xffu),
    ) / 255.0;
}

fn pack4(v: vec4<f32>) -> u32 {
    let c = clamp(v, vec4<f32>(0.0), vec4<f32>(1.0)) * 255.0 + 0.5;
    return (u32(c.x)) | (u32(c.y) << 8u) | (u32(c.z) << 16u) | (u32(c.w) << 24u);
}

// BT.601 limited range, which is what the C1's UVC path delivers.
fn ycbcr_to_rgb(y: f32, cb: f32, cr: f32) -> vec3<f32> {
    let yy = (y - 16.0 / 255.0) * (255.0 / 219.0);
    let u = (cb - 128.0 / 255.0) * (255.0 / 224.0);
    let v = (cr - 128.0 / 255.0) * (255.0 / 224.0);
    return vec3<f32>(
        yy + 1.402 * v,
        yy - 0.344136 * u - 0.714136 * v,
        yy + 1.772 * u,
    );
}

fn rgb_to_y(c: vec3<f32>) -> f32 {
    let y = 0.299 * c.r + 0.587 * c.g + 0.114 * c.b;
    return y * (219.0 / 255.0) + 16.0 / 255.0;
}

fn rgb_to_cbcr(c: vec3<f32>) -> vec2<f32> {
    let cb = -0.168736 * c.r - 0.331264 * c.g + 0.5 * c.b;
    let cr = 0.5 * c.r - 0.418688 * c.g - 0.081312 * c.b;
    return vec2<f32>(cb, cr) * (224.0 / 255.0) + 128.0 / 255.0;
}

fn luma(c: vec3<f32>) -> f32 {
    return dot(c, vec3<f32>(0.299, 0.587, 0.114));
}

fn contrast_about(c: vec3<f32>, amount: f32, pivot: f32) -> vec3<f32> {
    return (c - pivot) * amount + pivot;
}

// Approximations of Composer's named photo effects. These are deliberate
// reinterpretations, not ports: Apple's CIPhotoEffect curves are not public.
fn apply_look(c_in: vec3<f32>) -> vec3<f32> {
    var c = c_in;
    switch params.look {
        case 1u: { // process — cool shadows, lifted greens
            c = vec3<f32>(c.r * 0.92, c.g * 1.05, c.b * 1.08 + 0.02);
            c = contrast_about(c, 1.12, 0.5);
        }
        case 2u: { // chrome — punchy and bright
            c = contrast_about(c, 1.35, 0.52);
            c = pow(max(c, vec3<f32>(0.0)), vec3<f32>(0.92));
        }
        case 3u: { // fade — lifted blacks, soft
            c = c * 0.82 + 0.12;
            c = contrast_about(c, 0.85, 0.5);
        }
        case 4u: { // instant — warm
            c = vec3<f32>(c.r * 1.12 + 0.03, c.g * 1.02, c.b * 0.88);
            c = contrast_about(c, 1.1, 0.48);
        }
        case 5u: { // mono
            c = vec3<f32>(luma(c));
        }
        case 6u: { // noir — dramatic black and white
            var g = luma(c);
            g = (g - 0.5) * 1.45 + 0.45;
            g = pow(max(g, 0.0), 1.15);
            c = vec3<f32>(g);
        }
        case 7u: { // tonal — soft black and white
            var g = luma(c) * 0.9 + 0.08;
            g = (g - 0.5) * 0.9 + 0.5;
            c = vec3<f32>(g);
        }
        case 8u: { // transfer — warm midtones
            c = vec3<f32>(c.r * 1.08 + 0.04, c.g * 1.04 + 0.02, c.b * 0.9);
            c = contrast_about(c, 1.05, 0.5);
        }
        default: {}
    }
    return clamp(mix(c_in, c, params.strength), vec3<f32>(0.0), vec3<f32>(1.0));
}

@compute @workgroup_size(8, 8)
fn main(@builtin(global_invocation_id) gid: vec3<u32>) {
    let x = gid.x * 4u;
    let y = gid.y * 2u;
    if (x >= params.width || y >= params.height) {
        return;
    }

    let w = params.width;
    let uv_base = w * params.height;

    // One aligned u32 per luma row, one for the shared chroma pair.
    let y0_word = (y * w + x) >> 2u;
    let y1_word = ((y + 1u) * w + x) >> 2u;
    let uv_word = (uv_base + (y >> 1u) * w + x) >> 2u;

    let y0 = unpack4(src[y0_word]);
    let y1 = unpack4(src[y1_word]);
    let uv = unpack4(src[uv_word]);  // U0 V0 U1 V1

    var out0: vec4<f32>;
    var out1: vec4<f32>;
    var cb_sum = vec2<f32>(0.0);
    var cr_sum = vec2<f32>(0.0);

    for (var i = 0u; i < 4u; i = i + 1u) {
        // Columns 0,1 share the first chroma pair; columns 2,3 the second.
        let pair = i >> 1u;
        let cb = select(uv.x, uv.z, pair == 1u);
        let cr = select(uv.y, uv.w, pair == 1u);

        let top = apply_look(ycbcr_to_rgb(y0[i], cb, cr));
        let bot = apply_look(ycbcr_to_rgb(y1[i], cb, cr));

        out0[i] = rgb_to_y(top);
        out1[i] = rgb_to_y(bot);

        // Average the block's four graded pixels back down to one chroma pair.
        let ct = rgb_to_cbcr(top);
        let cbm = rgb_to_cbcr(bot);
        cb_sum[pair] = cb_sum[pair] + ct.x + cbm.x;
        cr_sum[pair] = cr_sum[pair] + ct.y + cbm.y;
    }

    dst[y0_word] = pack4(out0);
    dst[y1_word] = pack4(out1);
    dst[uv_word] = pack4(vec4<f32>(
        cb_sum.x * 0.25, cr_sum.x * 0.25,
        cb_sum.y * 0.25, cr_sum.y * 0.25,
    ));
}
