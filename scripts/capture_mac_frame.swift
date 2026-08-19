import AVFoundation
import AppKit
import CoreMedia

class Grab: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let session = AVCaptureSession()
    let out = AVCaptureVideoDataOutput()
    var done = false
    let path: String
    let wanted: String

    init(path: String, wanted: String) {
        self.path = path
        self.wanted = wanted
    }

    func start() {
        let devices = AVCaptureDevice.DiscoverySession(
            deviceTypes: [.external, .builtInWideAngleCamera],
            mediaType: .video,
            position: .unspecified
        ).devices
        print("devices: \(devices.map { $0.localizedName })")
        guard let dev = devices.first(where: { $0.localizedName == wanted }) else {
            fputs("NO_DEVICE \(wanted)\n", stderr)
            exit(2)
        }
        print("using \(dev.localizedName) suspended=\(dev.isSuspended)")
        do {
            let input = try AVCaptureDeviceInput(device: dev)
            session.beginConfiguration()
            session.sessionPreset = .hd1280x720
            if session.canAddInput(input) { session.addInput(input) }
            out.alwaysDiscardsLateVideoFrames = true
            out.setSampleBufferDelegate(self, queue: DispatchQueue(label: "cap"))
            if session.canAddOutput(out) { session.addOutput(out) }
            session.commitConfiguration()
            session.startRunning()
        } catch {
            fputs("INPUT_ERR \(error)\n", stderr)
            exit(3)
        }
    }

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        if done { return }
        guard let buf = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        done = true
        let w = CVPixelBufferGetWidth(buf)
        let h = CVPixelBufferGetHeight(buf)
        let ci = CIImage(cvPixelBuffer: buf)
        let rep = NSCIImageRep(ciImage: ci)
        let img = NSImage(size: NSSize(width: w, height: h))
        img.addRepresentation(rep)
        guard let tiff = img.tiffRepresentation,
              let bitmap = NSBitmapImageRep(data: tiff),
              let png = bitmap.representation(using: .png, properties: [:])
        else {
            fputs("ENCODE_FAIL\n", stderr)
            exit(4)
        }
        do {
            try png.write(to: URL(fileURLWithPath: path))
            print("FRAME \(w)x\(h) bytes=\(png.count) -> \(path)")
        } catch {
            fputs("WRITE_ERR \(error)\n", stderr)
            exit(4)
        }
        session.stopRunning()
        exit(0)
    }
}

let wanted = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "Opal C1"
let path = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "frame.png"
Grab(path: path, wanted: wanted).start()
RunLoop.main.run(until: Date().addingTimeInterval(10))
fputs("TIMEOUT\n", stderr)
exit(5)
