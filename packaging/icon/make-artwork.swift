// Draws the MediaTidy artwork: the app icon (packaging/MediaTidy.icns) and the GitHub social preview
// (packaging/icon/social-preview.png, upload in the repository settings).
//   swift packaging/icon/make-artwork.swift            (from the repository folder)
// Motif: a loose pile of photos with a neat one on top, in the app colours
// (blue = dry run, amber like a darkroom safelight = real run).
import CoreGraphics
import CoreText
import Foundation
import ImageIO
import UniformTypeIdentifiers

func rgb(_ hex: UInt32, _ alpha: CGFloat = 1) -> CGColor {
    CGColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255, alpha: alpha)
}

let blueTop = rgb(0x3D86AB), blueBottom = rgb(0x1E4B62)
let dry = rgb(0x2D6784), dryDark = rgb(0x1F4A60), amber = rgb(0xE39A2D)
let sky = rgb(0xE6EEF2), white = rgb(0xFFFFFF)

/// One photo card centred at (cx, cy), rotated by `angle` degrees; `photo` draws the picture.
func card(_ ctx: CGContext, cx: CGFloat, cy: CGFloat, angle: CGFloat, fill: CGColor, photo: Bool) {
    let w: CGFloat = 500, h: CGFloat = 390, border: CGFloat = 26
    ctx.saveGState()
    ctx.translateBy(x: cx, y: cy)
    ctx.rotate(by: angle * .pi / 180)
    let rect = CGRect(x: -w / 2, y: -h / 2, width: w, height: h)
    ctx.setShadow(offset: CGSize(width: 0, height: -14), blur: 30, color: rgb(0x0B1E28, 0.45))
    ctx.addPath(CGPath(roundedRect: rect, cornerWidth: 30, cornerHeight: 30, transform: nil))
    ctx.setFillColor(fill)
    ctx.fillPath()
    ctx.setShadow(offset: .zero, blur: 0, color: nil)
    if photo {
        let pic = rect.insetBy(dx: border, dy: border)
        ctx.addPath(CGPath(roundedRect: pic, cornerWidth: 12, cornerHeight: 12, transform: nil))
        ctx.clip()
        ctx.setFillColor(sky)
        ctx.fill(pic)
        ctx.setFillColor(amber)  // sun
        ctx.fillEllipse(in: CGRect(x: pic.maxX - 150, y: pic.maxY - 140, width: 92, height: 92))
        func mountain(_ points: [CGPoint], _ color: CGColor) {
            ctx.move(to: points[0])
            points.dropFirst().forEach { ctx.addLine(to: $0) }
            ctx.closePath()
            ctx.setFillColor(color)
            ctx.fillPath()
        }
        mountain([CGPoint(x: pic.minX - 10, y: pic.minY), CGPoint(x: pic.minX + 150, y: pic.minY + 210),
                  CGPoint(x: pic.minX + 330, y: pic.minY)], dry)
        mountain([CGPoint(x: pic.minX + 170, y: pic.minY), CGPoint(x: pic.minX + 330, y: pic.minY + 160),
                  CGPoint(x: pic.maxX + 10, y: pic.minY + 30), CGPoint(x: pic.maxX + 10, y: pic.minY)], dryDark)
    }
    ctx.restoreGState()
}

func bitmap(width: Int, height: Int) -> CGContext {
    let ctx = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                        space: CGColorSpace(name: CGColorSpace.sRGB)!,
                        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    ctx.interpolationQuality = .high
    return ctx
}

func drawIcon(size: Int) -> CGImage {
    let ctx = bitmap(width: size, height: size)
    ctx.scaleBy(x: CGFloat(size) / 1024, y: CGFloat(size) / 1024)
    drawIconArt(ctx)
    return ctx.makeImage()!
}

/// The icon in Apple's 1024 × 1024 grid.
func drawIconArt(_ ctx: CGContext) {
    // Rounded square on Apple's 1024 grid (824 pt body), with a soft drop shadow
    let body = CGRect(x: 100, y: 100, width: 824, height: 824)
    let shape = CGPath(roundedRect: body, cornerWidth: 186, cornerHeight: 186, transform: nil)
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -12), blur: 28, color: rgb(0x000000, 0.35))
    ctx.addPath(shape)
    ctx.setFillColor(blueBottom)
    ctx.fillPath()
    ctx.restoreGState()

    ctx.saveGState()
    ctx.addPath(shape)
    ctx.clip()
    let gradient = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
                              colors: [blueTop, blueBottom] as CFArray, locations: [0, 1])!
    ctx.drawLinearGradient(gradient, start: CGPoint(x: 512, y: 924), end: CGPoint(x: 512, y: 100), options: [])
    // Loose pile underneath, tidy photo on top
    card(ctx, cx: 492, cy: 470, angle: 13, fill: rgb(0xF6E2C0), photo: false)
    card(ctx, cx: 532, cy: 488, angle: -9, fill: rgb(0xD9E6EC), photo: false)
    card(ctx, cx: 512, cy: 500, angle: 0, fill: white, photo: true)
    ctx.restoreGState()
}

/// Text block from the top-left corner (CoreGraphics counts y from the bottom); returns its height.
@discardableResult
func text(_ ctx: CGContext, _ string: String, font: String, size: CGFloat, color: CGColor,
          x: CGFloat, top: CGFloat, width: CGFloat, height: CGFloat) -> CGFloat {
    let attrs: [NSAttributedString.Key: Any] = [
        NSAttributedString.Key(kCTFontAttributeName as String): CTFontCreateWithName(font as CFString, size, nil),
        NSAttributedString.Key(kCTForegroundColorAttributeName as String): color,
    ]
    let setter = CTFramesetterCreateWithAttributedString(NSAttributedString(string: string, attributes: attrs))
    let fit = CTFramesetterSuggestFrameSizeWithConstraints(setter, CFRange(), nil,
                                                           CGSize(width: width, height: height), nil)
    let frame = CTFramesetterCreateFrame(setter, CFRange(),
                                         CGPath(rect: CGRect(x: x, y: top - fit.height, width: width,
                                                             height: fit.height), transform: nil), nil)
    CTFrameDraw(frame, ctx)
    return fit.height
}

/// GitHub social preview: 1280 × 640, icon + name + one sentence on a calm background.
func drawSocialPreview() -> CGImage {
    let ctx = bitmap(width: 1280, height: 640)
    ctx.setFillColor(rgb(0xF5F6F4))
    ctx.fill(CGRect(x: 0, y: 0, width: 1280, height: 640))
    ctx.saveGState()
    ctx.translateBy(x: 40, y: 80)
    ctx.scaleBy(x: 480 / 1024, y: 480 / 1024)
    drawIconArt(ctx)
    ctx.restoreGState()
    let x: CGFloat = 560, width: CGFloat = 640
    var top: CGFloat = 470
    top -= text(ctx, "MediaTidy", font: "Futura-Bold", size: 96, color: rgb(0x23272B),
                x: x, top: top, width: width, height: 140) + 18
    top -= text(ctx, "Back up, find duplicates and sort photos and videos by date, camera and lens.",
                font: "HelveticaNeue", size: 38, color: rgb(0x40464D), x: x, top: top, width: width, height: 200) + 26
    text(ctx, "Free and open source · macOS 13 or later", font: "HelveticaNeue-Medium", size: 28,
         color: dry, x: x, top: top, width: width, height: 60)
    return ctx.makeImage()!
}

func writePNG(_ image: CGImage, to url: URL) {
    let dest = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil)!
    CGImageDestinationAddImage(dest, image, nil)
    guard CGImageDestinationFinalize(dest) else { fatalError("cannot write \(url.path)") }
}

let iconset = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("MediaTidy.iconset")
try? FileManager.default.removeItem(at: iconset)
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)
for base in [16, 32, 128, 256, 512] {
    writePNG(drawIcon(size: base), to: iconset.appendingPathComponent("icon_\(base)x\(base).png"))
    writePNG(drawIcon(size: base * 2), to: iconset.appendingPathComponent("icon_\(base)x\(base)@2x.png"))
}
writePNG(drawIcon(size: 1024), to: URL(fileURLWithPath: "packaging/icon/MediaTidy-1024.png"))
writePNG(drawSocialPreview(), to: URL(fileURLWithPath: "packaging/icon/social-preview.png"))

let iconutil = Process()
iconutil.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
iconutil.arguments = ["-c", "icns", iconset.path, "-o", "packaging/MediaTidy.icns"]
try iconutil.run()
iconutil.waitUntilExit()
guard iconutil.terminationStatus == 0 else { fatalError("iconutil failed") }
print("Wrote packaging/MediaTidy.icns, packaging/icon/MediaTidy-1024.png and packaging/icon/social-preview.png")
