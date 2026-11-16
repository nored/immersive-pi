import Foundation

struct ScanViewpoint {
    let position: [Double]
    let forward: [Double]

    init?(pose: [Double]) {
        guard pose.count == 16, pose.allSatisfy({$0.isFinite}) else { return nil }
        position = [pose[3],pose[7],pose[11]]
        let direction = [-pose[2],-pose[6],-pose[10]]
        let length = sqrt(direction.map{$0*$0}.reduce(0,+))
        guard length > 0.99 && length < 1.01 else { return nil }
        forward = direction.map{$0/length}
    }

    func distance(to other: ScanViewpoint) -> Double {
        sqrt(zip(position,other.position).map{pow($0.0-$0.1,2)}.reduce(0,+))
    }

    func angle(to other: ScanViewpoint) -> Double {
        acos(min(1,max(-1,zip(forward,other.forward).map{$0.0*$0.1}.reduce(0,+)))) * 180 / .pi
    }
}

struct AutomaticProjectorScan {
    private var candidate: String?
    private var anchor: ScanViewpoint?
    private var firstTime = 0.0
    private var lastTime = -Double.infinity
    private var samples = 0
    private var cooldownUntil = -Double.infinity
    private var previous: [String: [ScanViewpoint]] = [:]
    private(set) var instruction = "Point at a projector, then hold the phone still."

    mutating func observe(node: String?, view: ScanViewpoint?, timestamp: Double) -> String? {
        guard timestamp.isFinite, timestamp > lastTime else { return nil }
        let gap = timestamp-lastTime
        lastTime = timestamp
        guard let node, let view else {
            resetCandidate()
            instruction = "Point at a projector to recognise it."
            return nil
        }
        if (previous[node] ?? []).contains(where:{$0.distance(to:view)<0.35 && $0.angle(to:view)<12}) {
            resetCandidate()
            instruction = "This view is recorded. Move to another angle or the next projector."
            return nil
        }
        guard timestamp >= cooldownUntil else { return nil }
        if candidate != node || gap > 1.2 || anchor == nil || anchor!.distance(to:view)>0.04 || anchor!.angle(to:view)>3 {
            candidate = node; anchor = view; firstTime = timestamp; samples = 1
        } else { samples += 1 }
        instruction = "\(node) recognised — hold still to start scanning."
        guard samples >= 3 && timestamp-firstTime >= 1 else { return nil }
        resetCandidate()
        return node
    }

    mutating func complete(node: String, view: ScanViewpoint, timestamp: Double) {
        previous[node,default:[]].append(view)
        if previous[node]!.count > 16 { previous[node]!.removeFirst() }
        cooldownUntil = timestamp+2
        resetCandidate()
        instruction = "View recorded. Move to another angle or the next projector."
    }

    mutating func retry(timestamp: Double) {
        cooldownUntil = timestamp+3
        resetCandidate()
        instruction = "Hold still for the reference images. The scan will retry automatically."
    }

    private mutating func resetCandidate() {
        candidate = nil; anchor = nil; samples = 0
    }
}
