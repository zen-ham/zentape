// Engine stub: opens the hook's shared texture (handle from the named shared
// memory), reads frames into a staging texture, and verifies real pixels at full
// rate. Proves the engine can pull the game's frames cross-process at ~180.
use std::ffi::c_void;
use std::time::{Duration, Instant};
use windows::core::*;
use windows::Win32::Foundation::*;
use windows::Win32::Graphics::Direct3D::*;
use windows::Win32::Graphics::Direct3D11::*;
use windows::Win32::Graphics::Dxgi::Common::*;
use windows::Win32::System::Memory::*;

#[repr(C)]
struct SharedInfo {
    magic: u32,
    width: u32,
    height: u32,
    format: u32,
    handle: u64,
    present_count: u64,
    copied_count: u64,
}

fn main() -> Result<()> {
    unsafe {
        let shm = OpenFileMappingW(FILE_MAP_ALL_ACCESS.0, false, w!("zentape_capture_info"))?;
        let view = MapViewOfFile(shm, FILE_MAP_ALL_ACCESS, 0, 0, std::mem::size_of::<SharedInfo>());
        let info = view.Value as *const SharedInfo;

        let mut tries = 0;
        while ((*info).magic != 0x5A544341 || (*info).handle == 0) && tries < 250 {
            std::thread::sleep(Duration::from_millis(20));
            tries += 1;
        }
        let w = (*info).width;
        let h = (*info).height;
        let fmt = DXGI_FORMAT((*info).format as i32);
        let handle = (*info).handle;
        println!("shared info: {}x{} fmt={} handle={:#x}", w, h, fmt.0, handle);
        if handle == 0 {
            eprintln!("no shared handle (is the hook injected?)");
            std::process::exit(1);
        }

        let mut device: Option<ID3D11Device> = None;
        let mut ctx: Option<ID3D11DeviceContext> = None;
        D3D11CreateDevice(
            None, D3D_DRIVER_TYPE_HARDWARE, HMODULE::default(),
            D3D11_CREATE_DEVICE_FLAG(0), None, D3D11_SDK_VERSION,
            Some(&mut device), None, Some(&mut ctx),
        )?;
        let device = device.unwrap();
        let ctx = ctx.unwrap();

        let mut shared_opt: Option<ID3D11Texture2D> = None;
        device.OpenSharedResource(HANDLE(handle as usize as *mut c_void), &mut shared_opt)?;
        let shared = shared_opt.unwrap();
        println!("opened shared texture OK");

        let sdesc = D3D11_TEXTURE2D_DESC {
            Width: w, Height: h, MipLevels: 1, ArraySize: 1, Format: fmt,
            SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
            Usage: D3D11_USAGE_STAGING, BindFlags: 0,
            CPUAccessFlags: D3D11_CPU_ACCESS_READ.0 as u32, MiscFlags: 0,
        };
        let mut staging: Option<ID3D11Texture2D> = None;
        device.CreateTexture2D(&sdesc, None, Some(&mut staging))?;
        let staging = staging.unwrap();

        let c0 = (*info).copied_count;
        let start = Instant::now();
        let mut reads = 0u64;
        let mut distinct = 0u64;
        let mut last_px = u32::MAX;
        let mut nonblack = 0u64;
        while start.elapsed().as_secs_f64() < 5.0 {
            ctx.CopyResource(&staging, &shared);
            let mut m = D3D11_MAPPED_SUBRESOURCE::default();
            if ctx.Map(&staging, 0, D3D11_MAP_READ, 0, Some(&mut m)).is_ok() {
                let row = m.RowPitch as usize;
                let base = m.pData as *const u8;
                let off = (h as usize / 2) * row + (w as usize / 2) * 4;
                let px = *(base.add(off) as *const u32);
                if px != last_px {
                    distinct += 1;
                    last_px = px;
                }
                if px & 0x00FFFFFF != 0 {
                    nonblack += 1;
                }
                ctx.Unmap(&staging, 0);
            }
            reads += 1;
        }
        let dt = start.elapsed().as_secs_f64();
        let c1 = (*info).copied_count;
        println!("reader read-loop: {:.0}/s", reads as f64 / dt);
        println!("distinct frames seen: {:.0}/s", distinct as f64 / dt);
        println!("hook copied to shared: {:.0}/s", (c1 - c0) as f64 / dt);
        println!("non-black center pixel: {}/{} reads (last={:#010x})", nonblack, reads, last_px);
    }
    Ok(())
}
