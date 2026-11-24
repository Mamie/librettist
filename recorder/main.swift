import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

/// Lightweight, botless macOS meeting audio recorder using ScreenCaptureKit (macOS 15+).
/// Captures both system audio (remote participants) and microphone (your voice) into
/// separate WAV files without requiring any virtual audio driver (like BlackHole).
///
/// Usage:
///   meeting-recorder --output-dir <dir> [--no-mic] [--no-system] [--check-permissions]
///
/// Stop cleanly by sending SIGINT or SIGTERM.

final class StreamAudioWriter {
    let label: String
    let outputURL: URL
    private var writer: AVAssetWriter?
    private var input: AVAssetWriterInput?
    private var started = false
    private var closed = false
    private(set) var sampleCount = 0

    init(label: String, outputURL: URL) {
        self.label = label
        self.outputURL = outputURL
    }

    func append(_ sampleBuffer: CMSampleBuffer) {
        guard !closed else { return }
        guard CMSampleBufferDataIsReady(sampleBuffer),
              CMSampleBufferGetNumSamples(sampleBuffer) > 0 else { return }

        if writer == nil {
            guard setupWriter(from: sampleBuffer) else { return }
        }

        guard let writer = writer, let input = input else { return }
        if writer.status == .failed {
            fputs("[\(label)] Writer failed: \(writer.error?.localizedDescription ?? "unknown")\n", stderr)
            closed = true
            return
        }

        let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        if !started {
            writer.startSession(atSourceTime: pts)
            started = true
        }

        if input.isReadyForMoreMediaData {
            if input.append(sampleBuffer) {
                sampleCount += 1
            }
        }
    }

    private func setupWriter(from sampleBuffer: CMSampleBuffer) -> Bool {
        guard let formatDesc = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc) else {
            return false
        }
        let asbd = asbdPtr.pointee
        let sampleRate = asbd.mSampleRate > 0 ? asbd.mSampleRate : 48000.0
        let channels = max(1, Int(asbd.mChannelsPerFrame))

        try? FileManager.default.removeItem(at: outputURL)

        do {
            let assetWriter = try AVAssetWriter(outputURL: outputURL, fileType: .wav)
            let settings: [String: Any] = [
                AVFormatIDKey: kAudioFormatLinearPCM,
                AVSampleRateKey: sampleRate,
                AVNumberOfChannelsKey: channels,
                AVLinearPCMBitDepthKey: 16,
                AVLinearPCMIsFloatKey: false,
                AVLinearPCMIsBigEndianKey: false,
                AVLinearPCMIsNonInterleaved: false,
            ]
            let writerInput = AVAssetWriterInput(mediaType: .audio, outputSettings: settings)
            writerInput.expectsMediaDataInRealTime = true

            guard assetWriter.canAdd(writerInput) else {
                fputs("[\(label)] Cannot add audio writer input\n", stderr)
                return false
            }
            assetWriter.add(writerInput)
            guard assetWriter.startWriting() else {
                fputs("[\(label)] Failed to start writing: \(assetWriter.error?.localizedDescription ?? "unknown")\n", stderr)
                return false
            }

            self.writer = assetWriter
            self.input = writerInput
            fputs("[\(label)] Recording started (\(Int(sampleRate)) Hz, \(channels) ch) -> \(outputURL.path)\n", stderr)
            return true
        } catch {
            fputs("[\(label)] AssetWriter init error: \(error.localizedDescription)\n", stderr)
            return false
        }
    }

    func finish() async {
        closed = true
        guard let writer = writer, let input = input, started else {
            return
        }
        input.markAsFinished()
        await withCheckedContinuation { continuation in
            writer.finishWriting {
                continuation.resume()
            }
        }
        fputs("[\(label)] Saved \(sampleCount) audio buffers to \(outputURL.path)\n", stderr)
    }
}

final class MeetingCaptureDelegate: NSObject, SCStreamOutput, SCStreamDelegate {
    let systemWriter: StreamAudioWriter?
    let micWriter: StreamAudioWriter?
    let queue = DispatchQueue(label: "com.librettist.audio-writer")
    var onError: ((Error) -> Void)?

    init(systemWriter: StreamAudioWriter?, micWriter: StreamAudioWriter?) {
        self.systemWriter = systemWriter
        self.micWriter = micWriter
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        switch type {
        case .audio:
            guard let systemWriter = systemWriter else { return }
            queue.sync { systemWriter.append(sampleBuffer) }
        case .microphone:
            guard let micWriter = micWriter else { return }
            queue.sync { micWriter.append(sampleBuffer) }
        case .screen:
            break
        @unknown default:
            break
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fputs("[stream] Stopped with error: \(error.localizedDescription)\n", stderr)
        onError?(error)
    }
}

struct Options {
    var outputDir: String = "."
    var captureSystem: Bool = true
    var captureMic: Bool = true
    var checkPermissionsOnly: Bool = false
}

func parseArgs() -> Options {
    var opts = Options()
    let args = CommandLine.arguments
    var i = 1
    while i < args.count {
        switch args[i] {
        case "--output-dir", "-o":
            if i + 1 < args.count {
                opts.outputDir = args[i + 1]
                i += 1
            }
        case "--no-mic":
            opts.captureMic = false
        case "--no-system":
            opts.captureSystem = false
        case "--check-permissions":
            opts.checkPermissionsOnly = true
        case "--help", "-h":
            print("""
            Usage: meeting-recorder --output-dir <dir> [--no-mic] [--no-system] [--check-permissions]
            Captures macOS system audio and microphone into system.wav and mic.wav until SIGINT/SIGTERM.
            """)
            exit(0)
        default:
            break
        }
        i += 1
    }
    return opts
}

@main
struct MeetingRecorderApp {
    static func main() async {
        let opts = parseArgs()

        if opts.captureMic {
            let micStatus = AVCaptureDevice.authorizationStatus(for: .audio)
            if micStatus == .notDetermined {
                let granted = await AVCaptureDevice.requestAccess(for: .audio)
                if !granted {
                    fputs("WARNING: Microphone permission denied. Grant access in System Settings -> Privacy & Security -> Microphone.\n", stderr)
                }
            } else if micStatus == .denied || micStatus == .restricted {
                fputs("WARNING: Microphone permission denied. Grant access in System Settings -> Privacy & Security -> Microphone.\n", stderr)
            }
        }

        let content: SCShareableContent
        do {
            content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        } catch {
            fputs("ERROR: Failed to access ScreenCaptureKit: \(error.localizedDescription)\n", stderr)
            fputs("Grant 'Screen & System Audio Recording' permission to your terminal in System Settings -> Privacy & Security.\n", stderr)
            exit(2)
        }

        guard let display = content.displays.first else {
            fputs("ERROR: No display found for ScreenCaptureKit audio filter.\n", stderr)
            exit(3)
        }

        if opts.checkPermissionsOnly {
            print("OK: ScreenCaptureKit and audio permissions available (display: \(display.width)x\(display.height)).")
            exit(0)
        }

        let outURL = URL(fileURLWithPath: opts.outputDir, isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: outURL, withIntermediateDirectories: true)
        } catch {
            fputs("ERROR: Could not create output directory \(opts.outputDir): \(error.localizedDescription)\n", stderr)
            exit(4)
        }

        let filter = SCContentFilter(display: display, excludingWindows: [])
        let config = SCStreamConfiguration()
        config.capturesAudio = opts.captureSystem
        config.excludesCurrentProcessAudio = true
        config.sampleRate = 48000
        config.channelCount = 2
        if #available(macOS 15.0, *) {
            config.captureMicrophone = opts.captureMic
        } else if opts.captureMic {
            fputs("WARNING: Microphone capture via ScreenCaptureKit requires macOS 15.0+.\n", stderr)
        }
        // Minimal video footprint since we only need audio.
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        config.showsCursor = false

        let systemWriter = opts.captureSystem ? StreamAudioWriter(label: "system", outputURL: outURL.appendingPathComponent("system.wav")) : nil
        let micWriter = opts.captureMic ? StreamAudioWriter(label: "mic", outputURL: outURL.appendingPathComponent("mic.wav")) : nil
        let delegate = MeetingCaptureDelegate(systemWriter: systemWriter, micWriter: micWriter)
        let stream = SCStream(filter: filter, configuration: config, delegate: delegate)

        let sampleQueue = DispatchQueue(label: "com.librettist.scstream", qos: .userInitiated)
        do {
            try stream.addStreamOutput(delegate, type: .screen, sampleHandlerQueue: sampleQueue)
            if opts.captureSystem {
                try stream.addStreamOutput(delegate, type: .audio, sampleHandlerQueue: sampleQueue)
            }
            if opts.captureMic {
                if #available(macOS 15.0, *) {
                    try stream.addStreamOutput(delegate, type: .microphone, sampleHandlerQueue: sampleQueue)
                }
            }
            try await stream.startCapture()
        } catch {
            fputs("ERROR: Failed to start capture: \(error.localizedDescription)\n", stderr)
            exit(5)
        }

        fputs("RECORDING_STARTED pid=\(ProcessInfo.processInfo.processIdentifier)\n", stderr)
        fflush(stderr)

        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            var resumed = false
            let lock = NSLock()
            let resumeOnce = {
                lock.lock()
                defer { lock.unlock() }
                if !resumed {
                    resumed = true
                    continuation.resume()
                }
            }

            signal(SIGINT, SIG_IGN)
            signal(SIGTERM, SIG_IGN)

            let sigIntSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
            sigIntSource.setEventHandler { resumeOnce() }
            sigIntSource.resume()

            let sigTermSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
            sigTermSource.setEventHandler { resumeOnce() }
            sigTermSource.resume()

            delegate.onError = { _ in resumeOnce() }
            SignalHolder.sources = [sigIntSource, sigTermSource]
        }

        fputs("Stopping capture...\n", stderr)
        try? await stream.stopCapture()

        if let systemWriter = systemWriter {
            await systemWriter.finish()
        }
        if let micWriter = micWriter {
            await micWriter.finish()
        }
        fputs("RECORDING_STOPPED\n", stderr)
        fflush(stderr)
    }
}

enum SignalHolder {
    nonisolated(unsafe) static var sources: [DispatchSourceSignal] = []
}
