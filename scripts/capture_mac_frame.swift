import AVFoundation
import AppKit
import CoreMedia

// Frames are discarded for `settle` seconds before one is kept. The first
// frame off a freshly started session arrives mid-convergence: this camera
// keeps auto-white-balance and auto-focus locked on (see docs/camera-notes.md),
// so grabbing frame #1 gives every capture a different AWB state and the
// pairing the LUT fit depends on is broken before it starts.
let settle = ProcessInfo.processInfo.environment["CAPTURE_SETTLE"].flatMap(Double.init) ?? 3.0

class Grab: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let session = AVCaptureSession()
    let out = AVCaptureVideoDataOutput()
    var done = false
    var seen = 0
    var openedAt = Date()
    let path: String
    let wanted: String
    var dev: AVCaptureDevice?

    init(path: String, wanted: String) {
        self.path = path
        self.wanted = wanted
    }

    func start() {
        // Without camera authorization AVFoundation does not error - the
        // session starts cleanly and simply never delivers a frame. Surface
        // the TCC state and request access explicitly so the failure mode is
        // a visible prompt or message instead of a silent timeout.
        let auth = AVCaptureDevice.authorizationStatus(for: .video)
        print("camera authorization: \(auth.rawValue) (0=notDetermined 1=restricted 2=denied 3=authorized)")
        if auth == .notDetermined {
            let sem = DispatchSemaphore(value: 0)
            AVCaptureDevice.requestAccess(for: .video) { ok in
                print("access request -> \(ok)")
                sem.signal()
            }
            sem.wait()
        } else if auth != .authorized {
            fputs("NO_CAMERA_PERMISSION: grant camera access to this terminal in System Settings > Privacy & Security > Camera\n", stderr)
            exit(6)
        }
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
        self.dev = dev
        print("using \(dev.localizedName) suspended=\(dev.isSuspended)")
        report(dev)
        do {
            let input = try AVCaptureDeviceInput(device: dev)
            session.beginConfiguration()
            session.sessionPreset = .hd1280x720
            if session.canAddInput(input) { session.addInput(input) }
            out.alwaysDiscardsLateVideoFrames = true
            out.setSampleBufferDelegate(self, queue: DispatchQueue(label: "cap"))
            if session.canAddOutput(out) { session.addOutput(out) }
            session.commitConfiguration()
            openedAt = Date()
            session.startRunning()
            print("settling for \(settle)s before grabbing…")
        } catch {
            fputs("INPUT_ERR \(error)\n", stderr)
            exit(3)
        }
    }

    /// What this device will actually let us pin down. Anything reported
    /// unsupported here is a knob that has to be set in Composer's UI instead.
    func report(_ d: AVCaptureDevice) {
        let modes: [(String, AVCaptureDevice.ExposureMode)] =
            [("locked", .locked), ("auto", .autoExpose), ("continuous", .continuousAutoExposure)]
        let ex = modes.filter { d.isExposureModeSupported($0.1) }.map { $0.0 }
        let wbModes: [(String, AVCaptureDevice.WhiteBalanceMode)] =
            [("locked", .locked), ("auto", .autoWhiteBalance), ("continuous", .continuousAutoWhiteBalance)]
        let wb = wbModes.filter { d.isWhiteBalanceModeSupported($0.1) }.map { $0.0 }
        let fModes: [(String, AVCaptureDevice.FocusMode)] =
            [("locked", .locked), ("auto", .autoFocus), ("continuous", .continuousAutoFocus)]
        let fo = fModes.filter { d.isFocusModeSupported($0.1) }.map { $0.0 }
        print("exposure modes: \(ex.isEmpty ? ["none"] : ex)  current=\(d.exposureMode.rawValue)")
        print("whitebalance modes: \(wb.isEmpty ? ["none"] : wb)  current=\(d.whiteBalanceMode.rawValue)")
        print("focus modes: \(fo.isEmpty ? ["none"] : fo)  current=\(d.focusMode.rawValue)")
    }

    /// Lock whatever the device allows, so the remaining captures in a set
    /// cannot drift away from the baseline.
    func lockAll() {
        guard let d = dev else { return }
        do {
            try d.lockForConfiguration()
            if d.isExposureModeSupported(.locked) { d.exposureMode = .locked }
            if d.isWhiteBalanceModeSupported(.locked) { d.whiteBalanceMode = .locked }
            if d.isFocusModeSupported(.locked) { d.focusMode = .locked }
            d.unlockForConfiguration()
            print("locked: exposure=\(d.exposureMode.rawValue) wb=\(d.whiteBalanceMode.rawValue) focus=\(d.focusMode.rawValue)")
        } catch {
            print("lock unavailable: \(error)")
        }
    }

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        if done { return }
        seen += 1
        // Burn frames until the sensor has settled, then lock and take the next.
        if Date().timeIntervalSince(openedAt) < settle { return }
        if seen > 0 && dev?.exposureMode != .locked { lockAll() }
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
            print("FRAME \(w)x\(h) bytes=\(png.count) discarded=\(seen - 1) -> \(path)")
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
RunLoop.main.run(until: Date().addingTimeInterval(settle + 15))
fputs("TIMEOUT\n", stderr)
exit(5)
