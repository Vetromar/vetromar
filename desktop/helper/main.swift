// vetromar-helper — the thin native seam between the Python sidecar and
// Core Audio's process-tap APIs (macOS 14.2+). All policy lives in Python;
// this binary only observes and records.
//
// Modes (JSON lines on stdout, diagnostics on stderr, exits on stdin
// close or SIGTERM):
//   monitor --bundle-ids p1,p2,...   emit mic_start/mic_stop when a process
//                                    whose bundle id matches a watched prefix
//                                    starts/stops using the microphone
//   tap --bundle-prefix p --out x.wav   record the system-audio output of all
//                                    processes matching the prefix to a
//                                    16 kHz mono PCM16 WAV until stopped
//   selftest                         probe OS version + process enumeration

import AVFoundation
import AppKit
import CoreAudio
import Foundation

// MARK: - stdout/stderr plumbing

func emit(_ object: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: object),
          let line = String(data: data, encoding: .utf8)
    else { return }
    print(line)
    fflush(stdout)
}

func log(_ message: String) {
    FileHandle.standardError.write(Data(("vetromar-helper: " + message + "\n").utf8))
}

func fail(_ message: String) -> Never {
    emit(["event": "error", "message": message])
    exit(1)
}

// MARK: - Core Audio process objects

func audioProcessList() -> [AudioObjectID] {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyProcessObjectList,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var size: UInt32 = 0
    let system = AudioObjectID(kAudioObjectSystemObject)
    guard AudioObjectGetPropertyDataSize(system, &address, 0, nil, &size) == noErr else { return [] }
    let count = Int(size) / MemoryLayout<AudioObjectID>.size
    guard count > 0 else { return [] }
    var list = [AudioObjectID](repeating: 0, count: count)
    guard AudioObjectGetPropertyData(system, &address, 0, nil, &size, &list) == noErr else { return [] }
    return list
}

func processProperty<T>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, _ initial: T) -> T? {
    var address = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var value = initial
    var size = UInt32(MemoryLayout<T>.size)
    guard AudioObjectGetPropertyData(object, &address, 0, nil, &size, &value) == noErr else { return nil }
    return value
}

func processBundleID(_ object: AudioObjectID) -> String? {
    guard let cf: Unmanaged<CFString>? = processProperty(object, kAudioProcessPropertyBundleID, nil),
          let value = cf?.takeRetainedValue() as String?
    else { return nil }
    return value.isEmpty ? nil : value
}

func processPID(_ object: AudioObjectID) -> pid_t? {
    processProperty(object, kAudioProcessPropertyPID, pid_t(0))
}

func processIsRunningInput(_ object: AudioObjectID) -> Bool {
    (processProperty(object, kAudioProcessPropertyIsRunningInput, UInt32(0)) ?? 0) != 0
}

/// True when `bundleID` belongs to the watched prefix — exact match or a
/// helper of it (Chromium browsers run audio in `<prefix>.helper*` processes).
func matches(_ bundleID: String, prefix: String) -> Bool {
    bundleID == prefix || bundleID.hasPrefix(prefix + ".")
}

func matchingProcesses(prefix: String) -> [AudioObjectID] {
    audioProcessList().filter { object in
        guard let bundle = processBundleID(object) else { return false }
        return matches(bundle, prefix: prefix)
    }
}

// MARK: - lifecycle (stdin close / SIGTERM => clean exit)

var cleanupHandlers: [() -> Void] = []

func installLifecycleHandlers() {
    signal(SIGTERM, SIG_IGN)
    signal(SIGINT, SIG_IGN)
    for sig in [SIGTERM, SIGINT] {
        let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        source.setEventHandler {
            for handler in cleanupHandlers { handler() }
            exit(0)
        }
        source.resume()
        lifecycleSources.append(source)
    }
    // Parent-death safety: the sidecar holds our stdin open; EOF means it is
    // gone (or wants us gone), so shut down instead of orphaning a recorder.
    DispatchQueue.global().async {
        while readLine(strippingNewline: false) != nil {}
        DispatchQueue.main.async {
            for handler in cleanupHandlers { handler() }
            exit(0)
        }
    }
}

var lifecycleSources: [DispatchSourceSignal] = []

// MARK: - WAV output (16 kHz mono PCM16, header patched on close)

final class WavWriter {
    private let handle: FileHandle
    private var dataBytes: UInt32 = 0

    init?(path: String) {
        FileManager.default.createFile(atPath: path, contents: nil)
        guard let handle = FileHandle(forWritingAtPath: path) else { return nil }
        self.handle = handle
        handle.write(WavWriter.header(dataBytes: 0))
    }

    private static func header(dataBytes: UInt32) -> Data {
        var data = Data()
        func u32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { data.append(contentsOf: $0) } }
        func u16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { data.append(contentsOf: $0) } }
        data.append(contentsOf: "RIFF".utf8)
        u32(36 + dataBytes)
        data.append(contentsOf: "WAVE".utf8)
        data.append(contentsOf: "fmt ".utf8)
        u32(16)
        u16(1) // PCM
        u16(1) // mono
        u32(16000)
        u32(16000 * 2) // byte rate
        u16(2) // block align
        u16(16) // bits per sample
        data.append(contentsOf: "data".utf8)
        u32(dataBytes)
        return data
    }

    func append(_ chunk: Data) {
        handle.write(chunk)
        dataBytes += UInt32(chunk.count)
    }

    func close() {
        handle.seek(toFileOffset: 0)
        handle.write(WavWriter.header(dataBytes: dataBytes))
        try? handle.close()
    }
}

// MARK: - selftest

func runSelftest() -> Never {
    guard #available(macOS 14.2, *) else {
        emit(["event": "selftest", "ok": false, "reason": "requires macOS 14.2 or newer"])
        exit(1)
    }
    let processes = audioProcessList()
    if processes.isEmpty {
        emit(["event": "selftest", "ok": false, "reason": "no audio process objects visible"])
        exit(1)
    }
    emit(["event": "selftest", "ok": true, "processes": processes.count])
    exit(0)
}

// MARK: - monitor mode

@available(macOS 14.2, *)
func runMonitor(prefixes: [String]) -> Never {
    installLifecycleHandlers()
    emit(["event": "ready", "watching": prefixes])

    // prefix -> pid of a representative process currently running input
    var active: [String: pid_t] = [:]

    let timer = DispatchSource.makeTimerSource(queue: .main)
    timer.schedule(deadline: .now() + 1.0, repeating: 1.0)
    timer.setEventHandler {
        var seen: [String: (pid: pid_t, bundle: String)] = [:]
        for object in audioProcessList() {
            guard processIsRunningInput(object),
                  let bundle = processBundleID(object),
                  let prefix = prefixes.first(where: { matches(bundle, prefix: $0) })
            else { continue }
            if seen[prefix] == nil {
                seen[prefix] = (processPID(object) ?? -1, bundle)
            }
        }
        for (prefix, info) in seen where active[prefix] == nil {
            active[prefix] = info.pid
            let name = NSRunningApplication(processIdentifier: info.pid)?.localizedName ?? info.bundle
            emit([
                "event": "mic_start", "watch": prefix, "bundle_id": info.bundle,
                "pid": Int(info.pid), "name": name,
            ])
        }
        for (prefix, pid) in active where seen[prefix] == nil {
            active.removeValue(forKey: prefix)
            emit(["event": "mic_stop", "watch": prefix, "pid": Int(pid)])
        }
    }
    timer.resume()
    monitorTimer = timer
    dispatchMain()
}

var monitorTimer: DispatchSourceTimer?

// MARK: - tap mode

@available(macOS 14.2, *)
final class ProcessTapRecorder {
    let bundlePrefix: String
    let writer: WavWriter
    private let outFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true
    )!
    private let writeQueue = DispatchQueue(label: "vetromar.tap.write")

    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var ioProcID: AudioDeviceIOProcID?
    private var converter: AVAudioConverter?
    private var inFormat: AVAudioFormat?
    private var tappedProcesses: [AudioObjectID] = []
    private var announced = false

    init?(bundlePrefix: String, outPath: String) {
        guard let writer = WavWriter(path: outPath) else { return nil }
        self.bundlePrefix = bundlePrefix
        self.writer = writer
    }

    func start() {
        rebuild()
    }

    /// (Re)create the tap + aggregate device + IO proc for the current set of
    /// matching processes. Called at start and whenever the process set or the
    /// tap's stream format changes (device switches mid-call, app relaunch).
    func rebuild() {
        teardownAudio()

        tappedProcesses = matchingProcesses(prefix: bundlePrefix)
        let description: CATapDescription
        if tappedProcesses.isEmpty {
            // The target vanished momentarily (e.g. helper churn): fall back to
            // a global mixdown rather than record silence.
            log("no processes match \(bundlePrefix); falling back to global tap")
            description = CATapDescription(monoGlobalTapButExcludeProcesses: [])
        } else {
            description = CATapDescription(monoMixdownOfProcesses: tappedProcesses)
        }
        description.isPrivate = true
        description.muteBehavior = .unmuted

        var newTap = AudioObjectID(kAudioObjectUnknown)
        let tapStatus = AudioHardwareCreateProcessTap(description, &newTap)
        guard tapStatus == noErr else {
            log("AudioHardwareCreateProcessTap failed: \(tapStatus)")
            return
        }
        tapID = newTap

        guard let format = tapStreamFormat() else {
            log("could not read tap stream format")
            return
        }
        inFormat = format
        converter = AVAudioConverter(from: format, to: outFormat)

        let aggregateDescription: [String: Any] = [
            kAudioAggregateDeviceUIDKey as String: "com.vetromar.tap.\(UUID().uuidString)",
            kAudioAggregateDeviceNameKey as String: "Vetromar Meeting Tap",
            kAudioAggregateDeviceIsPrivateKey as String: true,
            kAudioAggregateDeviceIsStackedKey as String: false,
            kAudioAggregateDeviceTapAutoStartKey as String: true,
            kAudioAggregateDeviceSubDeviceListKey as String: [] as [[String: Any]],
            kAudioAggregateDeviceTapListKey as String: [
                [kAudioSubTapUIDKey as String: description.uuid.uuidString]
            ],
        ]
        var newAggregate = AudioObjectID(kAudioObjectUnknown)
        let aggStatus = AudioHardwareCreateAggregateDevice(aggregateDescription as CFDictionary, &newAggregate)
        guard aggStatus == noErr else {
            log("AudioHardwareCreateAggregateDevice failed: \(aggStatus)")
            return
        }
        aggregateID = newAggregate

        let procStatus = AudioDeviceCreateIOProcIDWithBlock(&ioProcID, aggregateID, writeQueue) {
            [weak self] _, inputData, _, _, _ in
            self?.handleInput(inputData)
        }
        guard procStatus == noErr, let procID = ioProcID else {
            log("AudioDeviceCreateIOProcIDWithBlock failed: \(procStatus)")
            return
        }
        let startStatus = AudioDeviceStart(aggregateID, procID)
        guard startStatus == noErr else {
            log("AudioDeviceStart failed: \(startStatus)")
            return
        }
    }

    private func tapStreamFormat() -> AVAudioFormat? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioTapPropertyFormat,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var asbd = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        guard AudioObjectGetPropertyData(tapID, &address, 0, nil, &size, &asbd) == noErr else { return nil }
        return AVAudioFormat(streamDescription: &asbd)
    }

    /// Runs on `writeQueue` (the IO proc's dispatch queue): convert the tap
    /// buffer to 16 kHz mono Int16 and append it to the WAV.
    private func handleInput(_ inputData: UnsafePointer<AudioBufferList>) {
        guard let inFormat, let converter else { return }
        let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inputData))
        guard let first = buffers.first(where: { $0.mData != nil }) else { return }
        let frames = AVAudioFrameCount(first.mDataByteSize / inFormat.streamDescription.pointee.mBytesPerFrame)
        guard frames > 0,
              let inBuffer = AVAudioPCMBuffer(pcmFormat: inFormat, frameCapacity: frames)
        else { return }
        inBuffer.frameLength = frames
        let destination = UnsafeMutableAudioBufferListPointer(inBuffer.mutableAudioBufferList)
        for (index, source) in buffers.enumerated() where index < destination.count {
            guard let src = source.mData, let dst = destination[index].mData else { continue }
            let bytes = min(source.mDataByteSize, destination[index].mDataByteSize)
            memcpy(dst, src, Int(bytes))
            destination[index].mDataByteSize = bytes
        }

        let ratio = outFormat.sampleRate / inFormat.sampleRate
        let capacity = AVAudioFrameCount(Double(frames) * ratio) + 64
        guard let outBuffer = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: capacity) else { return }
        var supplied = false
        var conversionError: NSError?
        converter.convert(to: outBuffer, error: &conversionError) { _, outStatus in
            if supplied {
                outStatus.pointee = .noDataNow
                return nil
            }
            supplied = true
            outStatus.pointee = .haveData
            return inBuffer
        }
        if let conversionError {
            log("conversion error: \(conversionError.localizedDescription)")
            return
        }
        guard outBuffer.frameLength > 0, let channel = outBuffer.int16ChannelData else { return }
        writer.append(Data(bytes: channel[0], count: Int(outBuffer.frameLength) * 2))
        if !announced {
            announced = true
            DispatchQueue.main.async { emit(["event": "tapping"]) }
        }
    }

    /// Cheap supervision: has the matching process set or stream format moved?
    func needsRebuild() -> Bool {
        let current = matchingProcesses(prefix: bundlePrefix)
        if !current.isEmpty, Set(current) != Set(tappedProcesses) { return true }
        if let format = tapStreamFormat(), let inFormat, format != inFormat { return true }
        return false
    }

    private func teardownAudio() {
        if aggregateID != AudioObjectID(kAudioObjectUnknown) {
            if let procID = ioProcID {
                AudioDeviceStop(aggregateID, procID)
                AudioDeviceDestroyIOProcID(aggregateID, procID)
                ioProcID = nil
            }
            AudioHardwareDestroyAggregateDevice(aggregateID)
            aggregateID = AudioObjectID(kAudioObjectUnknown)
        }
        if tapID != AudioObjectID(kAudioObjectUnknown) {
            AudioHardwareDestroyProcessTap(tapID)
            tapID = AudioObjectID(kAudioObjectUnknown)
        }
    }

    func stop() {
        teardownAudio()
        writeQueue.sync {} // drain in-flight writes before finalizing
        writer.close()
    }
}

@available(macOS 14.2, *)
func runTap(bundlePrefix: String, outPath: String) -> Never {
    guard let recorder = ProcessTapRecorder(bundlePrefix: bundlePrefix, outPath: outPath) else {
        fail("cannot open output file \(outPath)")
    }
    cleanupHandlers.append { recorder.stop() }
    installLifecycleHandlers()
    recorder.start()
    emit(["event": "ready", "out": outPath])

    let timer = DispatchSource.makeTimerSource(queue: .main)
    timer.schedule(deadline: .now() + 2.0, repeating: 2.0)
    timer.setEventHandler {
        if recorder.needsRebuild() {
            log("process set or format changed; rebuilding tap")
            recorder.rebuild()
        }
    }
    timer.resume()
    monitorTimer = timer
    dispatchMain()
}

// MARK: - entry

func argValue(_ name: String) -> String? {
    let args = CommandLine.arguments
    guard let index = args.firstIndex(of: name), index + 1 < args.count else { return nil }
    return args[index + 1]
}

let mode = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : ""
switch mode {
case "selftest":
    runSelftest()
case "monitor":
    guard #available(macOS 14.2, *) else { fail("requires macOS 14.2 or newer") }
    guard let raw = argValue("--bundle-ids"), !raw.isEmpty else { fail("monitor requires --bundle-ids") }
    let prefixes = raw.split(separator: ",").map { String($0).trimmingCharacters(in: .whitespaces) }
    runMonitor(prefixes: prefixes.filter { !$0.isEmpty })
case "tap":
    guard #available(macOS 14.2, *) else { fail("requires macOS 14.2 or newer") }
    guard let prefix = argValue("--bundle-prefix"), let out = argValue("--out") else {
        fail("tap requires --bundle-prefix and --out")
    }
    runTap(bundlePrefix: prefix, outPath: out)
default:
    fail("usage: vetromar-helper monitor|tap|selftest ...")
}
