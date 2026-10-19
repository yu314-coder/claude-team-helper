import AppKit
// Renders a 1024px app icon: gradient squircle with a white sync glyph over a sidebar.
let size: CGFloat = 1024
let img = NSImage(size: NSSize(width: size, height: size))
img.lockFocus()
let inset: CGFloat = 100
let rect = NSRect(x: inset, y: inset, width: size - 2*inset, height: size - 2*inset)
let path = NSBezierPath(roundedRect: rect, xRadius: 185, yRadius: 185)
NSGraphicsContext.current?.saveGraphicsState()
let shadow = NSShadow(); shadow.shadowBlurRadius = 28; shadow.shadowOffset = NSSize(width: 0, height: -12)
shadow.shadowColor = NSColor.black.withAlphaComponent(0.35); shadow.set()
NSColor.black.setFill(); path.fill()
NSGraphicsContext.current?.restoreGraphicsState()
NSGradient(colors: [NSColor(srgbRed: 0.85, green: 0.45, blue: 0.30, alpha: 1),
                    NSColor(srgbRed: 0.36, green: 0.25, blue: 0.70, alpha: 1)])!.draw(in: path, angle: -60)
// Sidebar panel
let panel = NSRect(x: rect.minX + 120, y: rect.minY + 150, width: 230, height: rect.height - 300)
NSColor.white.withAlphaComponent(0.22).setFill()
NSBezierPath(roundedRect: panel, xRadius: 40, yRadius: 40).fill()
NSColor.white.withAlphaComponent(0.85).setFill()
for i in 0..<4 {
    let y = panel.maxY - 90 - CGFloat(i) * 100
    NSBezierPath(roundedRect: NSRect(x: panel.minX + 40, y: y, width: panel.width - 80, height: 34), xRadius: 17, yRadius: 17).fill()
}
// Sync glyph
let cfg = NSImage.SymbolConfiguration(pointSize: 360, weight: .semibold)
    .applying(.init(paletteColors: [.white]))
if let sym = NSImage(systemSymbolName: "arrow.triangle.2.circlepath", accessibilityDescription: nil)?.withSymbolConfiguration(cfg) {
    let s = sym.size
    let r = NSRect(x: panel.maxX + (rect.maxX - panel.maxX - s.width)/2 - 10, y: rect.midY - s.height/2, width: s.width, height: s.height)
    sym.draw(in: r)
}
img.unlockFocus()
let rep = NSBitmapImageRep(data: img.tiffRepresentation!)!
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
