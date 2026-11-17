import AppKit

struct Brand: Decodable {
    let canvas: Double
    let background: String
    let foreground: String
    let inset: Double
    let radius: Double
    let stroke: Double
    let points: [[Double]]
}

func color(_ hex: String) -> NSColor {
    let value = UInt32(hex.dropFirst(), radix: 16)!
    return NSColor(srgbRed: CGFloat((value >> 16) & 255) / 255,
                   green: CGFloat((value >> 8) & 255) / 255,
                   blue: CGFloat(value & 255) / 255, alpha: 1)
}

let dir = CommandLine.arguments[1]
let brand = try JSONDecoder().decode(Brand.self, from: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2])))
for (name, px) in [("icon_16x16",16),("icon_16x16@2x",32),("icon_32x32",32),("icon_32x32@2x",64),("icon_128x128",128),("icon_128x128@2x",256),("icon_256x256",256),("icon_256x256@2x",512),("icon_512x512",512),("icon_512x512@2x",1024)] {
    let size = CGFloat(px), scale = size / brand.canvas
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let inset = brand.inset * scale
    let body = NSBezierPath(roundedRect: NSRect(x: inset, y: inset, width: size - 2*inset, height: size - 2*inset), xRadius: brand.radius * scale, yRadius: brand.radius * scale)
    color(brand.background).setFill()
    body.fill()
    let mark = NSBezierPath()
    for (index, point) in brand.points.enumerated() {
        let p = NSPoint(x: point[0] * scale, y: size - point[1] * scale)
        if index == 0 { mark.move(to: p) } else { mark.line(to: p) }
    }
    mark.lineWidth = brand.stroke * scale
    mark.lineCapStyle = .round
    mark.lineJoinStyle = .round
    color(brand.foreground).setStroke()
    mark.stroke()
    NSGraphicsContext.current = nil
    try rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: "\(dir)/\(name).png"))
}
