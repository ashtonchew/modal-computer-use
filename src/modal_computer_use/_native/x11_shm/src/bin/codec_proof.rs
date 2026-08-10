//! Deterministic, secret-free proof for one compile-time PNG codec variant.

use std::io::Cursor;
use std::time::Instant;

use _modal_computer_use_x11_shm::{codec_marker_for_proof, encode_rgb_for_proof};

const WIDTH: u16 = 1024;
const HEIGHT: u16 = 768;

fn fixture_rgb() -> Vec<u8> {
    let mut rgb = vec![0_u8; usize::from(WIDTH) * usize::from(HEIGHT) * 3];
    for y in 0..usize::from(HEIGHT) {
        for x in 0..usize::from(WIDTH) {
            let pixel = if y < 56 {
                if (x / 32) % 6 == 0 && (8..40).contains(&(y % 48)) {
                    (103, 148, 214)
                } else {
                    (31, 42, 58)
                }
            } else if (96..usize::from(HEIGHT) - 32).contains(&y) && (x / 256) % 3 != 2 {
                let card_y = y % 128;
                let card_x = x % 256;
                let text_run = (16..24).contains(&card_y)
                    || (42..49).contains(&card_y)
                    || (66..71).contains(&card_y);
                if text_run && (24..220).contains(&card_x) {
                    (64, 72, 86)
                } else if (8..244).contains(&card_x) {
                    (
                        (x.wrapping_mul(3) + y.wrapping_mul(2)) as u8,
                        (x.wrapping_mul(5) + y.wrapping_mul(3)) as u8,
                        (x.wrapping_mul(7) + y.wrapping_mul(5)) as u8,
                    )
                } else {
                    (250, 251, 253)
                }
            } else if (x / 64 + y / 32) % 2 == 0 {
                (232, 236, 242)
            } else {
                (244, 247, 250)
            };
            let offset = (y * usize::from(WIDTH) + x) * 3;
            rgb[offset..offset + 3].copy_from_slice(&[pixel.0, pixel.1, pixel.2]);
        }
    }
    rgb
}

fn fnv1a64(bytes: &[u8]) -> u64 {
    let mut hash = 0xcbf29ce484222325_u64;
    for byte in bytes {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x100000001b3);
    }
    hash
}

fn main() {
    let expected = fixture_rgb();
    let started = Instant::now();
    let encoded = encode_rgb_for_proof(&expected, WIDTH, HEIGHT).expect("codec proof encode");
    let encode_ms = started.elapsed().as_secs_f64() * 1000.0;

    let decoder = png::Decoder::new(Cursor::new(&encoded));
    let mut reader = decoder.read_info().expect("codec proof PNG header");
    let mut decoded = vec![0_u8; reader.output_buffer_size().expect("codec proof buffer size")];
    let info = reader.next_frame(&mut decoded).expect("codec proof PNG decode");
    decoded.truncate(info.buffer_size());
    let pixel_parity = info.width == u32::from(WIDTH)
        && info.height == u32::from(HEIGHT)
        && decoded == expected;
    assert!(pixel_parity, "codec proof decoded pixels or dimensions differ");

    println!(
        "{{\"codec\":\"{}\",\"width\":{},\"height\":{},\"payload_bytes\":{},\"encode_ms\":{:.6},\"decoded_pixel_bytes\":{},\"pixel_parity\":true,\"pixel_hash\":\"{:016x}\"}}",
        codec_marker_for_proof(),
        WIDTH,
        HEIGHT,
        encoded.len(),
        encode_ms,
        decoded.len(),
        fnv1a64(&decoded),
    );
}
