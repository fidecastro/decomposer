// NV12 in, look applied, NV12 out — one compute pass, no textures.
//
// Each invocation owns a 4x2 pixel block, which is the smallest region that
// lines up with both NV12's half-resolution chroma and 4-byte storage writes.
// That makes every read and write exactly one aligned u32, so no two
// invocations ever touch the same word and no atomics are needed.

struct Params {
    width: u32,          // output dimensions
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
    // Source dimensions: the capture may be larger than the output (4K in,
    // 1080p out), which is what makes zoom lossless up to their ratio.
    src_w: u32,
    src_h: u32,
    // Digital zoom: the crop window is src/zoom, positioned by pan in
    // [-1, 1] across the available margin. zoom 1 + pan 0 = identity.
    zoom: f32,
    pan_x: f32,
    pan_y: f32,
    // CLAHE: strength 0 disables; clip limits how far a tile may equalize.
    clahe: f32,
    clahe_clip: f32,
    _pad0: u32,
    _pad1: u32,
};

@group(0) @binding(0) var<uniform> params: Params;
@group(0) @binding(1) var<storage, read> src: array<u32>;
@group(0) @binding(2) var<storage, read_write> dst: array<u32>;
// Pre-scaled RGBA8, one pixel per u32, already at ov_w x ov_h.
@group(0) @binding(3) var<storage, read> overlay: array<u32>;
// Red varies fastest: index = r + g*size + b*size*size.
@group(0) @binding(4) var<storage, read> lut: array<vec4<f32>>;
// CLAHE working buffers: per-tile luma histograms and the mapping curves
// built from them. The histogram is cleared by the CDF pass after use, so no
// separate clear is needed between frames.
@group(0) @binding(5) var<storage, read_write> clahe_hist: array<atomic<u32>>;
@group(0) @binding(6) var<storage, read_write> clahe_lut: array<f32>;

const CLAHE_TX: u32 = 8u;
const CLAHE_TY: u32 = 8u;

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

// -- source sampling ---------------------------------------------------
// The input is NV12 at src_w x src_h; reads are byte fetches out of u32
// words. Bilinear taps: at zoom 1 with equal sizes the coordinates are
// integral and this degenerates to exact reads.

fn y_byte(x: u32, y: u32) -> f32 {
    let idx = y * params.src_w + x;
    let word = src[idx >> 2u];
    return f32((word >> ((idx & 3u) * 8u)) & 0xffu) / 255.0;
}

fn uv_bytes(cx: u32, cy: u32) -> vec2<f32> {
    // Chroma plane: src_h/2 rows of src_w bytes, U and V interleaved.
    let base = params.src_w * params.src_h;
    let idx = base + cy * params.src_w + cx * 2u;
    let w0 = src[idx >> 2u];
    let u = f32((w0 >> ((idx & 3u) * 8u)) & 0xffu);
    let idx_v = idx + 1u;
    let w1 = src[idx_v >> 2u];
    let v = f32((w1 >> ((idx_v & 3u) * 8u)) & 0xffu);
    return vec2<f32>(u, v) / 255.0;
}

fn sample_y(fx: f32, fy: f32) -> f32 {
    let mx = f32(params.src_w - 1u);
    let my = f32(params.src_h - 1u);
    let cx = clamp(fx, 0.0, mx);
    let cy = clamp(fy, 0.0, my);
    let x0 = u32(floor(cx)); let y0 = u32(floor(cy));
    let x1 = min(x0 + 1u, params.src_w - 1u);
    let y1 = min(y0 + 1u, params.src_h - 1u);
    let tx = fract(cx); let ty = fract(cy);
    let a = mix(y_byte(x0, y0), y_byte(x1, y0), tx);
    let b = mix(y_byte(x0, y1), y_byte(x1, y1), tx);
    return mix(a, b, ty);
}

fn sample_uv(fx: f32, fy: f32) -> vec2<f32> {
    let hw = params.src_w / 2u;
    let hh = params.src_h / 2u;
    let cxf = clamp(fx * 0.5, 0.0, f32(hw - 1u));
    let cyf = clamp(fy * 0.5, 0.0, f32(hh - 1u));
    let x0 = u32(floor(cxf)); let y0 = u32(floor(cyf));
    let x1 = min(x0 + 1u, hw - 1u);
    let y1 = min(y0 + 1u, hh - 1u);
    let tx = fract(cxf); let ty = fract(cyf);
    let a = mix(uv_bytes(x0, y0), uv_bytes(x1, y0), tx);
    let b = mix(uv_bytes(x0, y1), uv_bytes(x1, y1), tx);
    return mix(a, b, ty);
}

// Output pixel -> source coordinate: normalized position, mirrored if asked,
// then mapped into the zoomed crop window.
fn source_coord(px: u32, py: u32) -> vec2<f32> {
    var u = (f32(px) + 0.5) / f32(params.width);
    var v = (f32(py) + 0.5) / f32(params.height);
    if ((params.flip & 1u) != 0u) { u = 1.0 - u; }
    if ((params.flip & 2u) != 0u) { v = 1.0 - v; }
    let z = max(params.zoom, 1.0);
    let sw = f32(params.src_w);
    let sh = f32(params.src_h);
    let crop_w = sw / z;
    let crop_h = sh / z;
    let margin_x = (sw - crop_w) * 0.5;
    let margin_y = (sh - crop_h) * 0.5;
    let x0 = margin_x + clamp(params.pan_x, -1.0, 1.0) * margin_x;
    let y0 = margin_y + clamp(params.pan_y, -1.0, 1.0) * margin_y;
    return vec2<f32>(x0 + u * crop_w - 0.5, y0 + v * crop_h - 0.5);
}

// -- CLAHE --------------------------------------------------------------
// Contrast Limited Adaptive Histogram Equalization on the output-view luma:
// the histogram is built over exactly what will be shown (post zoom and
// mirror), so a zoomed crop gets its own contrast rather than the frame's.

@compute @workgroup_size(16, 16)
fn clahe_hist_main(@builtin(global_invocation_id) gid: vec3<u32>) {
    let px = gid.x;
    let py = gid.y;
    if (px >= params.width || py >= params.height) {
        return;
    }
    let sc = source_coord(px, py);
    let y = sample_y(sc.x, sc.y);
    let bin = min(u32(y * 255.0 + 0.5), 255u);
    let tx = min(px * CLAHE_TX / params.width, CLAHE_TX - 1u);
    let ty = min(py * CLAHE_TY / params.height, CLAHE_TY - 1u);
    atomicAdd(&clahe_hist[(ty * CLAHE_TX + tx) * 256u + bin], 1u);
}

var<workgroup> tile_bins: array<u32, 256>;
var<workgroup> tile_curve: array<f32, 256>;

// One workgroup per tile. The serial thread-0 section is ~1300 trivial ops
// per tile - not worth a parallel scan at this size.
@compute @workgroup_size(256)
fn clahe_cdf_main(
    @builtin(workgroup_id) wid: vec3<u32>,
    @builtin(local_invocation_id) lid: vec3<u32>,
) {
    let tile = wid.x;
    let i = lid.x;
    tile_bins[i] = atomicLoad(&clahe_hist[tile * 256u + i]);
    // Consumed: clear for the next frame's histogram pass.
    atomicStore(&clahe_hist[tile * 256u + i], 0u);
    workgroupBarrier();
    if (i == 0u) {
        var total: u32 = 0u;
        for (var k = 0u; k < 256u; k++) { total += tile_bins[k]; }
        let mean = f32(max(total, 1u)) / 256.0;
        let clip = max(u32(params.clahe_clip * mean), 1u);
        var excess: u32 = 0u;
        for (var k = 0u; k < 256u; k++) {
            if (tile_bins[k] > clip) {
                excess += tile_bins[k] - clip;
                tile_bins[k] = clip;
            }
        }
        let add = excess / 256u;
        var cum: u32 = 0u;
        for (var k = 0u; k < 256u; k++) {
            cum += tile_bins[k] + add;
            tile_curve[k] = f32(cum);
        }
        let denom = max(tile_curve[255], 1.0);
        for (var k = 0u; k < 256u; k++) {
            tile_curve[k] = tile_curve[k] / denom;
        }
    }
    workgroupBarrier();
    clahe_lut[tile * 256u + i] = tile_curve[i];
}

// Bilinear interpolation between the four surrounding tiles' curves - the
// standard CLAHE trick that hides the tile grid.
fn clahe_remap(px: u32, py: u32, y: f32) -> f32 {
    if (params.clahe <= 0.0) {
        return y;
    }
    let fx = (f32(px) + 0.5) / f32(params.width) * f32(CLAHE_TX) - 0.5;
    let fy = (f32(py) + 0.5) / f32(params.height) * f32(CLAHE_TY) - 0.5;
    let x0i = i32(floor(fx));
    let y0i = i32(floor(fy));
    let tx = fract(fx);
    let ty = fract(fy);
    let x0 = u32(clamp(x0i, 0, i32(CLAHE_TX) - 1));
    let x1 = u32(clamp(x0i + 1, 0, i32(CLAHE_TX) - 1));
    let y0 = u32(clamp(y0i, 0, i32(CLAHE_TY) - 1));
    let y1 = u32(clamp(y0i + 1, 0, i32(CLAHE_TY) - 1));
    let bin = min(u32(y * 255.0 + 0.5), 255u);
    let a = mix(clahe_lut[(y0 * CLAHE_TX + x0) * 256u + bin],
                clahe_lut[(y0 * CLAHE_TX + x1) * 256u + bin], tx);
    let b = mix(clahe_lut[(y1 * CLAHE_TX + x0) * 256u + bin],
                clahe_lut[(y1 * CLAHE_TX + x1) * 256u + bin], tx);
    return mix(y, mix(a, b, ty), params.clahe);
}

fn graded(px: u32, py: u32) -> vec3<f32> {
    let sc = source_coord(px, py);
    let y = clahe_remap(px, py, sample_y(sc.x, sc.y));
    let uv = sample_uv(sc.x, sc.y);
    let rgb = apply_look(ycbcr_to_rgb(y, uv.x, uv.y));
    return composite(px, py, rgb);
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

    var out0: vec4<f32>;
    var out1: vec4<f32>;
    var cb_sum = vec2<f32>(0.0);
    var cr_sum = vec2<f32>(0.0);

    for (var i = 0u; i < 4u; i = i + 1u) {
        let pair = i >> 1u;
        let top = graded(x + i, y);
        let bot = graded(x + i, y + 1u);

        out0[i] = rgb_to_y(top);
        out1[i] = rgb_to_y(bot);

        let ct = rgb_to_cbcr(top);
        let cbm = rgb_to_cbcr(bot);
        cb_sum[pair] = cb_sum[pair] + ct.x + cbm.x;
        cr_sum[pair] = cr_sum[pair] + ct.y + cbm.y;
    }

    // Writes stay at the invocation's own aligned block: no collisions.
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
