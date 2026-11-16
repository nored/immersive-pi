import Foundation
import CoreGraphics

enum CameraFraming {
    static func previewSize(native: CGSize, container: CGSize, expanded: Bool, portrait: Bool? = nil) -> CGSize {
        guard native.width > 0, native.height > 0, container.width > 0, container.height > 0 else { return .zero }
        // ARKit sensor images are landscape. Match the oriented image aspect
        // exactly so ARSCNView's aspect-fill transform has nothing to crop.
        let landscapeAspect = max(native.width, native.height) / min(native.width, native.height)
        let isPortrait = portrait ?? (container.height >= container.width)
        let aspect = isPortrait ? 1 / landscapeAspect : landscapeAspect
        let maxHeight = expanded ? max(0, container.height - 100) : container.height * 0.62
        let width = min(container.width, maxHeight * aspect)
        return CGSize(width:width, height:width / aspect)
    }
}
