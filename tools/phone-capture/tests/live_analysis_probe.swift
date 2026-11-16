import Foundation

@main
struct LiveAnalysisProbe {
    @MainActor static func main() async throws {
        let client=LiveAnalysisClient()
        client.address=CommandLine.arguments[1]
        let folder=URL(fileURLWithPath:CommandLine.arguments[2])
        let metadata=try JSONSerialization.jsonObject(with:Data(contentsOf:folder.appendingPathComponent("session.json"))) as! [String:Any]
        try await client.connect()
        try await client.begin(metadata:metadata)
        client.submit(folder:folder,name:"000000.json")
        for _ in 0..<100 {
            if client.uploaded==1 { break }
            try await Task.sleep(nanoseconds:50_000_000)
        }
        guard client.uploaded==1 && client.ready else {
            throw NSError(domain:"LiveProbe",code:1,userInfo:[NSLocalizedDescriptionKey:client.status])
        }
        print("Native phone transport: analysis handshake, RGB/depth/confidence upload, durable receipt and live guidance passed")
        print(client.instruction)
        client.close()
    }
}
