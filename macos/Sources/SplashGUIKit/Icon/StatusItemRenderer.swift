import AppKit

/// Draws the status item as one template image: the "S" glyph per state (docs/ui/10-menubar.md
/// §2.1, the DESIGN.md mark) followed by the optional `t/s`, MEM and GPU items (§2.2).
///
/// Drawn in code rather than shipped as assets so the package needs no resource pipeline;
/// the asset names in `StatusIcon.assetName` identify each variant. Every image has
/// `isTemplate = true`, so macOS inverts and dims it with the menu bar.
public enum StatusItemRenderer {
    public static let height: CGFloat = 18
    public static let glyphBox: CGFloat = 16

    public static func image(icon: StatusIcon, title: StatusTitle) -> NSImage {
        let items = layout(title)
        let width = height + items.reduce(0) { $0 + $1.width + 5 } + (items.isEmpty ? 0 : 1)
        let image = NSImage(size: NSSize(width: ceil(width), height: height), flipped: false) { _ in
            drawGlyph(icon, in: NSRect(x: 0, y: 0, width: height, height: height))
            var x = height + 4
            for item in items {
                item.draw(x)
                x += item.width + 5
            }
            return true
        }
        image.isTemplate = true
        return image
    }

    public static func glyphImage(_ icon: StatusIcon) -> NSImage {
        let image = NSImage(size: NSSize(width: height, height: height), flipped: false) { rect in
            drawGlyph(icon, in: rect)
            return true
        }
        image.isTemplate = true
        return image
    }

    // MARK: Glyph

    /// The DESIGN.md mark: display weight (900) at 72 % width in Archivo once the bundled font is
    /// registered (docs/ui/10 §2.1); the condensed black system font otherwise.
    static func glyphFont() -> NSFont {
        ArchivoFont.registeredFont(size: 17, weight: 900, width: 72)
            ?? NSFont.systemFont(ofSize: 17, weight: .black, width: .condensed)
    }

    static func drawGlyph(_ icon: StatusIcon, in rect: NSRect) {
        let box = NSRect(x: rect.midX - glyphBox / 2, y: rect.midY - glyphBox / 2, width: glyphBox, height: glyphBox)
        switch icon {
        case .stopped:
            drawS(in: box, filled: false, alpha: 1)
        case .offline:
            drawS(in: box, filled: false, alpha: 0.5)
        case .ready:
            drawS(in: box, filled: true, alpha: 1)
        case .generating(let on):
            drawS(in: box, filled: true, alpha: 1)
            if on { drawDot(in: box) }
        case .recovering(let on):
            drawS(in: box, filled: false, alpha: 1)
            if on { drawDot(in: box) }
        case .loading(let frame):
            drawS(in: box, filled: false, alpha: 1)
            drawOrbitDash(frame: frame, in: box)
        case .error:
            drawS(in: box, filled: true, alpha: 1)
            drawExclamationCutout(in: box)
        }
    }

    static func drawS(in box: NSRect, filled: Bool, alpha: CGFloat) {
        let color = NSColor.black.withAlphaComponent(alpha)
        var attrs: [NSAttributedString.Key: Any] = [.font: glyphFont()]
        if filled {
            attrs[.foregroundColor] = color
        } else {
            // Positive stroke width = outline only (percent of the point size): ≈1.5 pt.
            attrs[.strokeWidth] = 8.0
            attrs[.strokeColor] = color
        }
        let s = NSAttributedString(string: "S", attributes: attrs)
        let size = s.size()
        s.draw(at: NSPoint(x: box.midX - size.width / 2, y: box.midY - size.height / 2 + 0.5))
    }

    /// The 2 pt activity dot at the bottom-right corner.
    static func drawDot(in box: NSRect) {
        NSColor.black.setFill()
        NSBezierPath(ovalIn: NSRect(x: box.maxX - 3, y: box.minY, width: 3, height: 3)).fill()
    }

    /// A short dash orbiting the square bounding box; 12 frames per lap.
    static func drawOrbitDash(frame: Int, in box: NSRect) {
        let r = box.insetBy(dx: 0.5, dy: 0.5)
        let corners = [NSPoint(x: r.minX, y: r.maxY), NSPoint(x: r.maxX, y: r.maxY),
                       NSPoint(x: r.maxX, y: r.minY), NSPoint(x: r.minX, y: r.minY)]
        let side = r.width
        let perimeter = side * 4
        func point(at distance: CGFloat) -> NSPoint {
            var d = distance.truncatingRemainder(dividingBy: perimeter)
            if d < 0 { d += perimeter }
            let i = Int(d / side) % 4
            let t = (d - CGFloat(i) * side) / side
            let a = corners[i], b = corners[(i + 1) % 4]
            return NSPoint(x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t)
        }
        let start = perimeter * CGFloat(frame % StatusIcon.loadingFrameCount) / CGFloat(StatusIcon.loadingFrameCount)
        let path = NSBezierPath()
        path.lineWidth = 1.5
        path.move(to: point(at: start))
        for step in 1...4 { path.line(to: point(at: start + CGFloat(step) * 1.25)) }
        NSColor.black.setStroke()
        path.stroke()
    }

    /// An exclamation mark cut out of the lower half of the filled S.
    static func drawExclamationCutout(in box: NSRect) {
        guard let ctx = NSGraphicsContext.current else { return }
        let cut = NSRect(x: box.maxX - 7, y: box.minY - 1, width: 8, height: 10)
        ctx.saveGraphicsState()
        ctx.compositingOperation = .destinationOut
        NSColor.black.setFill()
        NSBezierPath(rect: cut).fill()
        ctx.restoreGraphicsState()
        NSColor.black.setFill()
        NSBezierPath(rect: NSRect(x: cut.midX - 1, y: cut.minY + 4.5, width: 2, height: 5.5)).fill()
        NSBezierPath(rect: NSRect(x: cut.midX - 1, y: cut.minY + 1, width: 2, height: 2)).fill()
    }

    // MARK: Title items

    struct Item {
        var width: CGFloat
        var draw: (CGFloat) -> Void
    }

    static func titleFont() -> NSFont {
        NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .medium)
    }

    static func textItem(_ text: String) -> Item {
        let s = NSAttributedString(string: text, attributes: [.font: titleFont(), .foregroundColor: NSColor.black])
        let size = s.size()
        return Item(width: ceil(size.width)) { x in
            s.draw(at: NSPoint(x: x, y: (height - size.height) / 2))
        }
    }

    /// `MEM ▮▮▮▯▯`: label + five 2 × 8 pt cells, 1 pt apart; empty cells at 30 % alpha.
    static func gaugeItem(label: String, filled: Int) -> Item {
        let text = textItem(label)
        let cells: CGFloat = 5 * 2 + 4 * 1
        return Item(width: text.width + 3 + cells) { x in
            text.draw(x)
            var cx = x + text.width + 3
            for i in 0..<5 {
                (i < filled ? NSColor.black : NSColor.black.withAlphaComponent(0.3)).setFill()
                NSBezierPath(rect: NSRect(x: cx, y: (height - 8) / 2, width: 2, height: 8)).fill()
                cx += 3
            }
        }
    }

    static func layout(_ title: StatusTitle) -> [Item] {
        var items: [Item] = []
        if title.loadingEllipsis { items.append(textItem("…")) }
        if let t = title.tokps { items.append(textItem(t)) }
        if let m = title.memoryCells { items.append(gaugeItem(label: "MEM", filled: m)) }
        if let g = title.gpuCells { items.append(gaugeItem(label: "GPU", filled: g)) }
        return items
    }
}
