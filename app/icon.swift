import AppKit

struct Brand: Decodable {
    let image: String
}

let dir = CommandLine.arguments[1]
let brandURL = URL(fileURLWithPath: CommandLine.arguments[2])
let brand = try JSONDecoder().decode(Brand.self, from: Data(contentsOf: brandURL))
let imageURL = brandURL.deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent(brand.image)
guard let artwork = NSImage(contentsOf: imageURL) else {
    fatalError("Cannot load app icon: \(imageURL.path)")
}
for (name, px) in [("icon_16x16",16),("icon_16x16@2x",32),("icon_32x32",32),("icon_32x32@2x",64),("icon_128x128",128),("icon_128x128@2x",256),("icon_256x256",256),("icon_256x256@2x",512),("icon_512x512",512),("icon_512x512@2x",1024)] {
    let size = CGFloat(px)
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    NSGraphicsContext.current?.imageInterpolation = .high
    artwork.draw(in: NSRect(x: 0, y: 0, width: size, height: size), from: .zero, operation: .copy, fraction: 1)
    NSGraphicsContext.restoreGraphicsState()
    try rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: "\(dir)/\(name).png"))
}
