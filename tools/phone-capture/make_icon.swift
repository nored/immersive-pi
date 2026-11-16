// Deterministic vector drawing for the prototype's app icon.
import Foundation
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers

let size = 1024
let color = CGColorSpace(name: CGColorSpace.sRGB)!
let context = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8,
    bytesPerRow: size * 4, space: color,
    bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
context.setFillColor(CGColor(srgbRed: 0.04, green: 0.08, blue: 0.14, alpha: 1))
context.fill(CGRect(x: 0, y: 0, width: size, height: size))
context.setStrokeColor(CGColor(srgbRed: 0.25, green: 0.84, blue: 0.77, alpha: 1))
context.setLineWidth(30); context.setLineCap(.round)
for (x, y, dx, dy) in [(210.0,210.0,1.0,1.0),(814,210,-1,1),(210,814,1,-1),(814,814,-1,-1)] {
    context.move(to: CGPoint(x: x, y: y + dy * 120))
    context.addLine(to: CGPoint(x: x, y: y))
    context.addLine(to: CGPoint(x: x + dx * 120, y: y))
    context.strokePath()
}
context.setLineWidth(16)
context.strokeEllipse(in: CGRect(x: 344, y: 344, width: 336, height: 336))
context.setStrokeColor(CGColor(srgbRed: 0.88, green: 0.97, blue: 1, alpha: 1))
for angle in stride(from: 0.0, to: Double.pi * 2, by: Double.pi / 6) {
    context.move(to: CGPoint(x: 512 + cos(angle) * 190, y: 512 + sin(angle) * 190))
    context.addLine(to: CGPoint(x: 512 + cos(angle) * 222, y: 512 + sin(angle) * 222))
    context.strokePath()
}
context.setFillColor(CGColor(srgbRed: 0.88, green: 0.97, blue: 1, alpha: 1))
context.fillEllipse(in: CGRect(x: 484, y: 484, width: 56, height: 56))
let url = URL(fileURLWithPath: CommandLine.arguments[1])
let destination = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil)!
CGImageDestinationAddImage(destination, context.makeImage()!, nil)
guard CGImageDestinationFinalize(destination) else { fatalError("Cannot write icon") }
