import AppKit
let dir = CommandLine.arguments[1]
for (name, px) in [("icon_16x16",16),("icon_16x16@2x",32),("icon_32x32",32),("icon_32x32@2x",64),("icon_128x128",128),("icon_128x128@2x",256),("icon_256x256",256),("icon_256x256@2x",512),("icon_512x512",512),("icon_512x512@2x",1024)] {
    let s = CGFloat(px)
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let inset = s * 0.09
    let body = NSBezierPath(roundedRect: NSRect(x: inset, y: inset, width: s - 2*inset, height: s - 2*inset), xRadius: s*0.2, yRadius: s*0.2)
    NSColor(red: 0.12, green: 0.12, blue: 0.11, alpha: 1).setFill(); body.fill()
    let colors = [NSColor(red: 0.31, green: 0.81, blue: 0.53, alpha: 1), NSColor(red: 0.94, green: 0.71, blue: 0.31, alpha: 1), NSColor(red: 0.49, green: 0.61, blue: 0.96, alpha: 1)]
    for (i, c) in colors.enumerated() {
        let y = s*0.62 - CGFloat(i)*s*0.19
        c.setFill(); NSBezierPath(ovalIn: NSRect(x: s*0.24, y: y, width: s*0.11, height: s*0.11)).fill()
        NSColor(white: 0.85, alpha: 1).setFill()
        NSBezierPath(roundedRect: NSRect(x: s*0.42, y: y + s*0.025, width: s*(0.34 - CGFloat(i)*0.06), height: s*0.06), xRadius: s*0.03, yRadius: s*0.03).fill()
    }
    NSGraphicsContext.current = nil
    try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: "\(dir)/\(name).png"))
}
