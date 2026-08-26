// Render the colour target through Composer's own look implementations,
// producing exact reference pairs without a camera in the loop.
//
// Why this exists: Composer 1.4.4 refuses to *apply* looks on Intel Macs (the
// Filters UI is absent and `effects.filters` in a preset is ignored at render
// time — "This feature requires an M1 or later system"), so the
// photograph-the-screen protocol in docs/reference-capture.md cannot even be
// started on such a machine. But the looks themselves are ordinary code in the
// installed app:
//
//   - G1/D1/Q1/S1/X1 are unary MetalPetal fragment shaders
//     (MTG1Fragment, ...) in OpalCameraVideoService's default.metallib —
//     one input texture, one sampler, no other parameters. Any Metal GPU can
//     run them, Intel included.
//   - The eight named looks are Apple's public CIPhotoEffect* Core Image
//     filters, exactly as recorded in the app's *_pipeline.json files.
//
// So instead of measuring the looks through a monitor and a camera sensor —
// inheriting that display's gamut, the sensor's response, and every clipped
// patch — we run the chart through the real shaders and save what comes out.
// The pairs are pixel-exact and cover the whole cube.
//
// Hygiene: this reads Opal's metallib from the locally installed app at run
// time and saves only the *output images*. Nothing of Opal's is copied,
// linked in, or redistributed — the references measure what the looks do,
// which is what the whole capture protocol was for.
//
// Usage:
//   swiftc -O scripts/render_looks_mac.swift -o scripts/render_looks_mac
//   ./scripts/render_looks_mac references/color-target.png references/
//
// Values pass through untouched as 8-bit sRGB-encoded bytes: textures are
// rgba8Unorm (not _srgb) and the CIContext works in device RGB, matching how
// VideoService hands video frames to these same filters.

import AppKit
import CoreImage
import Foundation
import Metal

let args = CommandLine.arguments
let inPath = args.count > 1 ? args[1] : "references/color-target.png"
let outDir = args.count > 2 ? args[2] : "references"
let metallib = "/Applications/Opal Composer.app/Contents/XPCServices/OpalCameraVideoService.xpc/Contents/Resources/default.metallib"

// ---- load the target as raw RGBA bytes, no colour conversion ----
guard let src = NSImage(contentsOfFile: inPath),
      let cg = src.cgImage(forProposedRect: nil, context: nil, hints: nil)
else { fputs("cannot read \(inPath)\n", stderr); exit(1) }
let W = cg.width, H = cg.height
let cs = CGColorSpace(name: CGColorSpace.sRGB)!
var rgba = [UInt8](repeating: 0, count: W * H * 4)
let ctx = CGContext(data: &rgba, width: W, height: H, bitsPerComponent: 8,
                    bytesPerRow: W * 4, space: cs,
                    bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
ctx.draw(cg, in: CGRect(x: 0, y: 0, width: W, height: H))

func writePNG(_ bytes: [UInt8], _ name: String) {
    var b = bytes
    let c = CGContext(data: &b, width: W, height: H, bitsPerComponent: 8,
                      bytesPerRow: W * 4, space: cs,
                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    let img = c.makeImage()!
    let url = URL(fileURLWithPath: "\(outDir)/\(name)")
    let dest = CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil)!
    CGImageDestinationAddImage(dest, img, nil)
    CGImageDestinationFinalize(dest)
    print("wrote \(url.path)")
}

// ---- baseline: the identity pair ----
writePNG(rgba, "look-off.png")

// ---- the five custom Metal looks ----
guard let dev = MTLCreateSystemDefaultDevice(),
      let lib = try? dev.makeLibrary(filepath: metallib)
else { fputs("cannot load \(metallib)\n", stderr); exit(1) }

let vsrc = """
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 position [[position]]; float2 textureCoordinate; };
vertex VOut passVertex(uint vid [[vertex_id]]) {
    float2 pos[4] = { {-1,-1},{1,-1},{-1,1},{1,1} };
    float2 tc[4]  = { {0,1},{1,1},{0,0},{1,0} };
    VOut o; o.position = float4(pos[vid],0,1); o.textureCoordinate = tc[vid]; return o;
}
"""
let vfn = (try! dev.makeLibrary(source: vsrc, options: nil)).makeFunction(name: "passVertex")!
let queue = dev.makeCommandQueue()!

let texDesc = MTLTextureDescriptor.texture2DDescriptor(
    pixelFormat: .rgba8Unorm, width: W, height: H, mipmapped: false)
texDesc.usage = [.shaderRead]
let inTex = dev.makeTexture(descriptor: texDesc)!
inTex.replace(region: MTLRegionMake2D(0, 0, W, H), mipmapLevel: 0,
              withBytes: rgba, bytesPerRow: W * 4)

let outDesc = MTLTextureDescriptor.texture2DDescriptor(
    pixelFormat: .rgba8Unorm, width: W, height: H, mipmapped: false)
outDesc.usage = [.renderTarget]
let sampDesc = MTLSamplerDescriptor()
sampDesc.minFilter = .linear
sampDesc.magFilter = .linear
let sampler = dev.makeSamplerState(descriptor: sampDesc)!

let metalLooks = ["G1": "MTG1Fragment", "D1": "MTD1Fragment", "Q1": "MTQ1Fragment",
                  "S1": "MTS1Fragment", "X1": "MTX1Fragment"]
for (look, fname) in metalLooks.sorted(by: { $0.key < $1.key }) {
    guard let ffn = lib.makeFunction(name: fname) else {
        fputs("missing \(fname)\n", stderr); continue
    }
    let pd = MTLRenderPipelineDescriptor()
    pd.vertexFunction = vfn
    pd.fragmentFunction = ffn
    pd.colorAttachments[0].pixelFormat = .rgba8Unorm
    let pso = try! dev.makeRenderPipelineState(descriptor: pd)
    let outTex = dev.makeTexture(descriptor: outDesc)!
    let rp = MTLRenderPassDescriptor()
    rp.colorAttachments[0].texture = outTex
    rp.colorAttachments[0].loadAction = .clear
    rp.colorAttachments[0].storeAction = .store
    let cb = queue.makeCommandBuffer()!
    let enc = cb.makeRenderCommandEncoder(descriptor: rp)!
    enc.setRenderPipelineState(pso)
    enc.setFragmentTexture(inTex, index: 0)
    enc.setFragmentSamplerState(sampler, index: 0)
    enc.drawPrimitives(type: .triangleStrip, vertexStart: 0, vertexCount: 4)
    enc.endEncoding()
    cb.commit()
    cb.waitUntilCompleted()
    var out = [UInt8](repeating: 0, count: W * H * 4)
    outTex.getBytes(&out, bytesPerRow: W * 4,
                    from: MTLRegionMake2D(0, 0, W, H), mipmapLevel: 0)
    writePNG(out, "look-\(look).png")
}

// ---- the eight Core Image looks ----
let ciLooks = ["chrome": "CIPhotoEffectChrome", "fade": "CIPhotoEffectFade",
               "instant": "CIPhotoEffectInstant", "mono": "CIPhotoEffectMono",
               "noir": "CIPhotoEffectNoir", "process": "CIPhotoEffectProcess",
               "tonal": "CIPhotoEffectTonal", "transfer": "CIPhotoEffectTransfer"]
// Device-RGB working space: bytes in == bytes the filter sees, matching the
// video path. sRGB output space would double-convert.
let ciCtx = CIContext(options: [.workingColorSpace: NSNull(), .outputColorSpace: NSNull()])
let base = CIImage(cgImage: ctx.makeImage()!)
for (look, filterName) in ciLooks.sorted(by: { $0.key < $1.key }) {
    guard let f = CIFilter(name: filterName) else {
        fputs("missing \(filterName)\n", stderr); continue
    }
    f.setValue(base, forKey: kCIInputImageKey)
    guard let outImg = f.outputImage,
          let outCG = ciCtx.createCGImage(outImg, from: CGRect(x: 0, y: 0, width: W, height: H))
    else { fputs("render failed \(look)\n", stderr); continue }
    var out = [UInt8](repeating: 0, count: W * H * 4)
    let c = CGContext(data: &out, width: W, height: H, bitsPerComponent: 8,
                      bytesPerRow: W * 4, space: cs,
                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    c.draw(outCG, in: CGRect(x: 0, y: 0, width: W, height: H))
    writePNG(out, "look-\(look).png")
}
print("done: 1 baseline + \(metalLooks.count) Metal + \(ciLooks.count) Core Image looks")
