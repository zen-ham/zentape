// Injected hook DLL. Hooks IDXGISwapChain::Present and, on each present, copies
// the game's backbuffer into a shared GPU texture that our separate engine process
// opens and reads -- no GPU->CPU readback in the game, ~free. A named shared-memory
// block carries the texture's shared handle + dimensions/format + counters so the
// engine knows what to open and at what rate frames arrive.
//
// Phase 2 uses a legacy shared handle (no keyed mutex) -- fine for proving the
// engine can read pixels at full rate; the engine phase adds keyed-mutex sync.
use std::ffi::c_void;
use std::io::Write;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use retour::GenericDetour;
use windows::core::*;
use windows::Win32::Foundation::*;
use windows::Win32::Graphics::Direct3D::*;
use windows::Win32::Graphics::Direct3D11::*;
use windows::Win32::Graphics::Dxgi::Common::*;
use windows::Win32::Graphics::Dxgi::*;
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::System::Memory::*;
use windows::Win32::UI::WindowsAndMessaging::*;

type PresentFn = unsafe extern "system" fn(*mut c_void, u32, u32) -> i32;

// Per-process SHM name so multiple hooked processes (e.g. a game + the Steam
// overlay) never collide on one mapping; the engine opens the one for its target.

#[repr(C)]
struct SharedInfo {
    magic: u32,
    width: u32,
    height: u32,
    format: u32,
    handle: u64, // legacy D3D shared handle (valid cross-process, same adapter)
    present_count: u64,
    copied_count: u64,
    hwnd: u64, // the swapchain's OutputWindow -- engine tests it for fullscreen/foreground
    pid: u32,
    _pad: u32,
}

static COUNT: AtomicU64 = AtomicU64::new(0);
static mut ORIG: Option<GenericDetour<PresentFn>> = None;

struct HookState {
    device: ID3D11Device,
    context: ID3D11DeviceContext,
    shared: Option<ID3D11Texture2D>,
    info: *mut SharedInfo,
}

thread_local! {
    static STATE: std::cell::RefCell<Option<HookState>> = std::cell::RefCell::new(None);
}

fn log(msg: &str) {
    let path = std::env::temp_dir().join("zentape_hook.log");
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(f, "{}", msg);
    }
}

unsafe fn map_shared_info() -> *mut SharedInfo {
    let name = format!("zentape_capture_info_{}\0", std::process::id());
    let wide: Vec<u16> = name.encode_utf16().collect();
    match CreateFileMappingW(
        INVALID_HANDLE_VALUE,
        None,
        PAGE_READWRITE,
        0,
        std::mem::size_of::<SharedInfo>() as u32,
        PCWSTR(wide.as_ptr()),
    ) {
        Ok(h) => {
            let v = MapViewOfFile(h, FILE_MAP_ALL_ACCESS, 0, 0, std::mem::size_of::<SharedInfo>());
            v.Value as *mut SharedInfo
        }
        Err(_) => std::ptr::null_mut(),
    }
}

unsafe fn ensure_shared(st: &mut HookState, backbuffer: &ID3D11Texture2D) {
    if st.shared.is_some() {
        return;
    }
    let mut desc = D3D11_TEXTURE2D_DESC::default();
    backbuffer.GetDesc(&mut desc);

    let sdesc = D3D11_TEXTURE2D_DESC {
        Width: desc.Width,
        Height: desc.Height,
        MipLevels: 1,
        ArraySize: 1,
        Format: desc.Format,
        SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
        Usage: D3D11_USAGE_DEFAULT,
        BindFlags: (D3D11_BIND_RENDER_TARGET.0 | D3D11_BIND_SHADER_RESOURCE.0) as u32,
        CPUAccessFlags: 0,
        MiscFlags: D3D11_RESOURCE_MISC_SHARED.0 as u32,
    };
    let mut tex: Option<ID3D11Texture2D> = None;
    if st.device.CreateTexture2D(&sdesc, None, Some(&mut tex)).is_err() {
        log("CreateTexture2D(shared) failed");
        return;
    }
    let tex = tex.unwrap();
    let res: IDXGIResource = match tex.cast() {
        Ok(r) => r,
        Err(_) => {
            log("cast IDXGIResource failed");
            return;
        }
    };
    let handle = match res.GetSharedHandle() {
        Ok(h) => h,
        Err(e) => {
            log(&format!("GetSharedHandle failed: {:?}", e));
            return;
        }
    };
    if !st.info.is_null() {
        (*st.info).magic = 0x5A544341; // 'ZTCA'
        (*st.info).width = desc.Width;
        (*st.info).height = desc.Height;
        (*st.info).format = desc.Format.0 as u32;
        (*st.info).handle = handle.0 as u64;
    }
    st.shared = Some(tex);
    log(&format!(
        "shared texture {}x{} fmt={} handle={:#x}",
        desc.Width, desc.Height, desc.Format.0, handle.0 as u64
    ));
}

extern "system" fn present_detour(sc: *mut c_void, sync: u32, flags: u32) -> i32 {
    COUNT.fetch_add(1, Ordering::Relaxed);
    unsafe {
        if let Some(swapchain) = IDXGISwapChain::from_raw_borrowed(&sc) {
            STATE.with(|cell| {
                let mut b = cell.borrow_mut();
                if b.is_none() {
                    if let Ok(device) = swapchain.GetDevice::<ID3D11Device>() {
                        if let Ok(context) = device.GetImmediateContext() {
                            *b = Some(HookState {
                                device,
                                context,
                                shared: None,
                                info: map_shared_info(),
                            });
                        }
                    }
                }
                if let Some(st) = b.as_mut() {
                    if let Ok(backbuffer) = swapchain.GetBuffer::<ID3D11Texture2D>(0) {
                        ensure_shared(st, &backbuffer);
                        if let Some(shared) = &st.shared {
                            st.context.CopyResource(shared, &backbuffer);
                            st.context.Flush();
                            if !st.info.is_null() {
                                (*st.info).copied_count += 1;
                                (*st.info).present_count = COUNT.load(Ordering::Relaxed);
                                if let Ok(scd) = swapchain.GetDesc() {
                                    (*st.info).hwnd = scd.OutputWindow.0 as usize as u64;
                                }
                                (*st.info).pid = std::process::id();
                            }
                        }
                    }
                }
            });
        }
        ORIG.as_ref().unwrap().call(sc, sync, flags)
    }
}

unsafe extern "system" fn dummy_wndproc(h: HWND, m: u32, w: WPARAM, l: LPARAM) -> LRESULT {
    DefWindowProcW(h, m, w, l)
}

unsafe fn find_present() -> Option<PresentFn> {
    let hmod = GetModuleHandleW(None).ok()?;
    let hinst = HINSTANCE(hmod.0);
    let cls = w!("zentape_hook_dummy");
    let wc = WNDCLASSW {
        lpfnWndProc: Some(dummy_wndproc),
        hInstance: hinst,
        lpszClassName: cls,
        ..Default::default()
    };
    RegisterClassW(&wc);
    let hwnd = CreateWindowExW(
        WINDOW_EX_STYLE(0), cls, w!("d"), WS_OVERLAPPEDWINDOW,
        0, 0, 2, 2, None, None, Some(hinst), None,
    ).ok()?;
    let desc = DXGI_SWAP_CHAIN_DESC {
        BufferDesc: DXGI_MODE_DESC {
            Width: 2, Height: 2,
            Format: DXGI_FORMAT_R8G8B8A8_UNORM,
            RefreshRate: DXGI_RATIONAL { Numerator: 60, Denominator: 1 },
            ScanlineOrdering: DXGI_MODE_SCANLINE_ORDER_UNSPECIFIED,
            Scaling: DXGI_MODE_SCALING_UNSPECIFIED,
        },
        SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
        BufferUsage: DXGI_USAGE_RENDER_TARGET_OUTPUT,
        BufferCount: 1,
        OutputWindow: hwnd,
        Windowed: TRUE,
        SwapEffect: DXGI_SWAP_EFFECT_DISCARD,
        Flags: 0,
    };
    let mut sc: Option<IDXGISwapChain> = None;
    let mut dev: Option<ID3D11Device> = None;
    D3D11CreateDeviceAndSwapChain(
        None, D3D_DRIVER_TYPE_HARDWARE, HMODULE::default(),
        D3D11_CREATE_DEVICE_FLAG(0), None, D3D11_SDK_VERSION,
        Some(&desc), Some(&mut sc), Some(&mut dev), None, None,
    ).ok()?;
    let sc = sc?;
    let raw = sc.as_raw() as *const *const usize;
    let vtbl = *raw;
    let present = *vtbl.add(8);
    let _ = DestroyWindow(hwnd);
    Some(std::mem::transmute::<usize, PresentFn>(present))
}

fn hook_main() {
    log(&format!("--- hook attached, pid={} ---", std::process::id()));
    unsafe {
        match find_present() {
            Some(present) => match GenericDetour::<PresentFn>::new(present, present_detour) {
                Ok(d) => {
                    let _ = d.enable();
                    ORIG = Some(d);
                    log("Present hooked OK");
                }
                Err(e) => {
                    log(&format!("detour error: {:?}", e));
                    return;
                }
            },
            None => {
                log("find_present failed");
                return;
            }
        }
    }
    let mut last = Instant::now();
    let mut lastp = 0u64;
    loop {
        std::thread::sleep(Duration::from_secs(1));
        let c = COUNT.load(Ordering::Relaxed);
        let dt = last.elapsed().as_secs_f64();
        log(&format!("hook present fps: {:.1}", (c - lastp) as f64 / dt));
        lastp = c;
        last = Instant::now();
    }
}

#[no_mangle]
extern "system" fn DllMain(_h: HINSTANCE, reason: u32, _r: *mut c_void) -> bool {
    if reason == 1 {
        std::thread::spawn(hook_main);
    }
    true
}
