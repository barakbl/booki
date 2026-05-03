//! Render the three LED colors to PNGs in /tmp so you can preview them
//! without launching the systray:
//!     cargo run --example render_leds
//! → /tmp/booki-led-{green,orange,red}.png

use booki_manager::menu::{led_icon, LedColor};
use image::{ImageBuffer, RgbaImage};

fn main() {
    for (name, color) in [
        ("green",  LedColor::Green),
        ("orange", LedColor::Orange),
        ("red",    LedColor::Red),
    ] {
        let icon = led_icon(color);
        // tray_icon::Icon doesn't expose its bytes; re-render via the same
        // routine but capture the buffer directly.
        let (w, h) = (22u32, 22u32);
        let rgba = render_rgba(color, w, h);
        let img: RgbaImage = ImageBuffer::from_raw(w, h, rgba)
            .expect("rgba buffer matches dimensions");
        let path = format!("/tmp/booki-led-{}.png", name);
        img.save(&path).expect("write PNG");
        println!("wrote {}  (icon ok: {})", path, format!("{:?}", &icon).len() > 0);
    }
}

/// Mirror of menu::led_icon's pixel logic. Kept inline here so the example
/// can produce a debug PNG without exposing the buffer through the public
/// `Icon` type.
fn render_rgba(color: LedColor, size_w: u32, size_h: u32) -> Vec<u8> {
    assert_eq!(size_w, size_h);
    let size = size_w;
    let mut rgba = vec![0u8; (size * size * 4) as usize];
    let cx = (size as f32 - 1.0) / 2.0;
    let cy = (size as f32 - 1.0) / 2.0;
    let r  = (size as f32) * 0.40;
    let edge = 1.0;
    let (r8, g8, b8) = match color {
        LedColor::Green  => (0x2e, 0xc4, 0x6b),
        LedColor::Orange => (0xf5, 0xa6, 0x23),
        LedColor::Red    => (0xe5, 0x3a, 0x3a),
    };
    for y in 0..size {
        for x in 0..size {
            let dx = x as f32 - cx;
            let dy = y as f32 - cy;
            let d  = (dx * dx + dy * dy).sqrt();
            let cov = ((r - d) / edge).clamp(0.0, 1.0);
            if cov <= 0.0 { continue; }
            let hi = (-dx - dy) / (r * 1.4);
            let bias = (hi.clamp(-0.5, 1.0) * 0.25).max(0.0);
            let lift = |c: u8| {
                let v = c as f32 + (255.0 - c as f32) * bias;
                v.clamp(0.0, 255.0) as u8
            };
            let i = ((y * size + x) * 4) as usize;
            rgba[i]     = lift(r8);
            rgba[i + 1] = lift(g8);
            rgba[i + 2] = lift(b8);
            rgba[i + 3] = (cov * 255.0) as u8;
        }
    }
    rgba
}
