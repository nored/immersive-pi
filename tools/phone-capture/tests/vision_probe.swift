import Foundation
import Vision

let request = VNDetectBarcodesRequest()
request.symbologies = [.qr]
try VNImageRequestHandler(url: URL(fileURLWithPath: CommandLine.arguments[1]), options: [:]).perform([request])
let observations = (request.results ?? []).map { value -> [String: Any] in
    ["payload": value.payloadStringValue ?? "",
     "corners": [value.topLeft, value.topRight, value.bottomRight, value.bottomLeft].map { [$0.x, $0.y] }]
}
let data = try JSONSerialization.data(withJSONObject: observations, options: [.prettyPrinted, .sortedKeys])
print(String(data: data, encoding: .utf8)!)
