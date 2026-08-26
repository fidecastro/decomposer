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
    // bit 0: mirror horizontally, bit 1: mirror vertically.
    // Both together is a 180 degree rotation, which needs no size change and
    // so can be toggled live without tearing down the virtual camera.
    flip: u32,
    // Overlay placement, in output pixels. ov_w == 0 means no overlay.
    ov_x: u32,
    ov_y: u32,
    ov_w: u32,
    ov_h: u32,
    ov_opacity: f32,
    /// Edge length of the loaded 3D LUT; 0 means use the built-in look.
    lut_size: u32,
    _pad0: u32,
};

@group(0) @binding(0) var<uniform> params: Params;
@group(0) @binding(1) var<storage, read> src: array<u32>;
@group(0) @binding(2) var<storage, read_write> dst: array<u32>;
// Pre-scaled RGBA8, one pixel per u32, already at ov_w x ov_h.
@group(0) @binding(3) var<storage, read> overlay: array<u32>;
// Red varies fastest: index = r + g*size + b*size*size.
@group(0) @binding(4) var<storage, read> lut: array<vec4<f32>>;

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

fn lut_at(r: u32, g: u32, b: u32) -> vec3<f32> {
    let n = params.lut_size;
    return lut[r + g * n + b * n * n].rgb;
}

// Trilinear between the eight surrounding grid points. The tables are measured
// at every one of their own grid points, so interpolation only ever happens
// between real measurements.
fn sample_lut(c: vec3<f32>) -> vec3<f32> {
    let n = f32(params.lut_size);
    let p = clamp(c, vec3<f32>(0.0), vec3<f32>(1.0)) * (n - 1.0);
    let lo = floor(p);
    let f = p - lo;
    let hi = min(lo + 1.0, vec3<f32>(n - 1.0));

    let r0 = u32(lo.r); let r1 = u32(hi.r);
    let g0 = u32(lo.g); let g1 = u32(hi.g);
    let b0 = u32(lo.b); let b1 = u32(hi.b);

    let c00 = mix(lut_at(r0, g0, b0), lut_at(r1, g0, b0), f.r);
    let c10 = mix(lut_at(r0, g1, b0), lut_at(r1, g1, b0), f.r);
    let c01 = mix(lut_at(r0, g0, b1), lut_at(r1, g0, b1), f.r);
    let c11 = mix(lut_at(r0, g1, b1), lut_at(r1, g1, b1), f.r);
    return mix(mix(c00, c10, f.g), mix(c01, c11, f.g), f.b);
}

// A LUT, when one is loaded, is Composer's actual transform rather than an
// approximation of it. The built-in curves below remain as a fallback for when
// the LUT files are not installed.
fn apply_look(c_in: vec3<f32>) -> vec3<f32> {
    if (params.lut_size > 0u) {
        return clamp(
            mix(c_in, sample_lut(c_in), params.strength),
            vec3<f32>(0.0), vec3<f32>(1.0),
        );
    }
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

// Composite the overlay in linear-ish RGB, before the trip back to YCbCr.
// Doing it here means the overlay is graded-over rather than graded, so a logo
// keeps its own colours whatever look is applied, and the alpha edge lands in
// RGB where it belongs instead of being smeared by chroma subsampling.
fn composite(px: u32, py: u32, rgb: vec3<f32>) -> vec3<f32> {
    if (params.ov_w == 0u || px < params.ov_x || py < params.ov_y) {
        return rgb;
    }
    let lx = px - params.ov_x;
    let ly = py - params.ov_y;
    if (lx >= params.ov_w || ly >= params.ov_h) {
        return rgb;
    }
    let word = overlay[ly * params.ov_w + lx];
    let src = vec3<f32>(
        f32( word        & 0xffu),
        f32((word >>  8u) & 0xffu),
        f32((word >> 16u) & 0xffu),
    ) / 255.0;
    let alpha = f32((word >> 24u) & 0xffu) / 255.0 * params.ov_opacity;
    return mix(rgb, src, clamp(alpha, 0.0, 1.0));
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

    let flip_h = (params.flip & 1u) != 0u;
    let flip_v = (params.flip & 2u) != 0u;

    // Read from the mirrored block. Both axes stay block-aligned: widths are a
    // multiple of four and the block is two rows tall, so every read is still
    // one aligned u32 and the write side is untouched.
    var sx = x;
    var sy = y;
    if (flip_h) { sx = w - 4u - x; }
    if (flip_v) { sy = params.height - 2u - y; }

    let s0_word = (sy * w + sx) >> 2u;
    let s1_word = ((sy + 1u) * w + sx) >> 2u;
    let suv_word = (uv_base + (sy >> 1u) * w + sx) >> 2u;

    var y0 = unpack4(src[s0_word]);
    var y1 = unpack4(src[s1_word]);
    var uv = unpack4(src[suv_word]);  // U0 V0 U1 V1

    if (flip_v) {
        // The block's two rows swap; chroma is shared between them.
        let t = y0;
        y0 = y1;
        y1 = t;
    }
    if (flip_h) {
        y0 = vec4<f32>(y0.w, y0.z, y0.y, y0.x);
        y1 = vec4<f32>(y1.w, y1.z, y1.y, y1.x);
        // Columns 0,1 now come from the source's right-hand pair.
        uv = vec4<f32>(uv.z, uv.w, uv.x, uv.y);
    }

    var out0: vec4<f32>;
    var out1: vec4<f32>;
    var cb_sum = vec2<f32>(0.0);
    var cr_sum = vec2<f32>(0.0);

    for (var i = 0u; i < 4u; i = i + 1u) {
        let pair = i >> 1u;
        let cb = select(uv.x, uv.z, pair == 1u);
        let cr = select(uv.y, uv.w, pair == 1u);

        // Overlay coordinates are output-space, so a mirrored image does not
        // drag the logo along with it.
        let top = composite(x + i, y, apply_look(ycbcr_to_rgb(y0[i], cb, cr)));
        let bot = composite(x + i, y + 1u, apply_look(ycbcr_to_rgb(y1[i], cb, cr)));

        out0[i] = rgb_to_y(top);
        out1[i] = rgb_to_y(bot);

        let ct = rgb_to_cbcr(top);
        let cbm = rgb_to_cbcr(bot);
        cb_sum[pair] = cb_sum[pair] + ct.x + cbm.x;
        cr_sum[pair] = cr_sum[pair] + ct.y + cbm.y;
    }

    // Writes stay at the invocation's own block, so no two threads collide.
    let d0_word = (y * w + x) >> 2u;
    let d1_word = ((y + 1u) * w + x) >> 2u;
    let duv_word = (uv_base + (y >> 1u) * w + x) >> 2u;

    dst[d0_word] = pack4(out0);
    dst[d1_word] = pack4(out1);
    dst[duv_word] = pack4(vec4<f32>(
        cb_sum.x * 0.25, cr_sum.x * 0.25,
        cb_sum.y * 0.25, cr_sum.y * 0.25,
    ));
}
