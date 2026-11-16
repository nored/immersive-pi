import AVFoundation
import Combine
import UIKit

@MainActor
final class ScanGuidance: ObservableObject {
    @Published var spoken = true
    private let voice = AVSpeechSynthesizer()
    private var lastText = ""
    private var lastTime = Date.distantPast
    func say(_ text:String, immediate:Bool = false) {
        guard spoken, !text.isEmpty, text != lastText else { return }
        guard immediate || Date().timeIntervalSince(lastTime)>6 else { return }
        lastText = text; lastTime = Date()
        if immediate { voice.stopSpeaking(at:.immediate) }
        let utterance = AVSpeechUtterance(string:text)
        utterance.voice = AVSpeechSynthesisVoice(language:"en-GB")
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate
        voice.speak(utterance)
    }
    func referenceCapture(_ active:Bool) {
        UINotificationFeedbackGenerator().notificationOccurred(active ? .warning : .success)
        say(active ? "Hold still while I capture the reference images." : "You can move slowly again.",immediate:true)
    }
    func stop() { voice.stopSpeaking(at:.immediate);lastText = "" }
}
