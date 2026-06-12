// zentape capture engine -- Phase 3c.
//
// Owns ONE D3D11 device and TWO capture sources that feed ONE NV12 output:
//   * DDA   -- DXGI Desktop Duplication of the primary output.
//   * HOOK  -- the game's backbuffer, copied by the injected hook into a shared
//              GPU texture (handle published in the "zentape_capture_info" SHM).
// Each source has its own GPU VideoProcessor path (BGRA/RGBA -> NV12 + scale to
// the target size) writing into the same NV12 target texture. The selected
// frame is read back and written as tightly-packed NV12 to stdout; a separate
// ffmpeg does nvenc + mpegts (Python's existing path). Timing is delegated to
// ffmpeg (`-use_wallclock_as_timestamps 1 -fps_mode cfr -r FPS`).
//
// `--source dda|hook|auto`. Phase 3c uses manual dda/hook; `auto` (Phase 4)
// switches by foreground/fullscreen state. Sharing ONE output texture + one
// readback/stdout stage is what makes the source switch seamless downstream.
use std::ffi::c_void;
use std::io::Write;
use std::mem::ManuallyDrop;
use std::time::Instant;

use windows::core::*;
use windows::Win32::Foundation::*;
use windows::Win32::Graphics::Direct3D::Fxc::*;
use windows::Win32::Graphics::Direct3D::*;
use windows::Win32::Graphics::Direct3D11::*;
use windows::Win32::Graphics::Dwm::{DwmGetWindowAttribute, DWMWA_CLOAKED};
use windows::Win32::Graphics::Dxgi::Common::*;
use windows::Win32::Graphics::Dxgi::*;
use windows::Win32::System::Diagnostics::Debug::WriteProcessMemory;
use windows::Win32::System::LibraryLoader::{GetModuleHandleW, GetProcAddress};
use windows::Win32::System::Memory::*;
use windows::Win32::System::Threading::{
    CreateRemoteThread, OpenProcess, WaitForSingleObject, PROCESS_ALL_ACCESS,
};
use windows::Win32::Graphics::Gdi::{DeleteObject, HGDIOBJ};
use windows::Win32::UI::HiDpi::{
    SetProcessDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
};
use windows::Win32::UI::WindowsAndMessaging::{
    DrawIconEx, GetClassNameW, GetCursorInfo, GetForegroundWindow, GetIconInfo, GetTopWindow,
    GetWindow, GetWindowLongPtrW, GetWindowRect, GetWindowThreadProcessId, IsWindowVisible,
    CURSORINFO, CURSOR_SHOWING, DI_NORMAL, GW_HWNDNEXT, GWL_EXSTYLE, HCURSOR, HICON, ICONINFO,
    WS_EX_NOACTIVATE, WS_EX_TRANSPARENT,
};

#[repr(C)]
#[derive(Clone, Copy)]
struct SharedInfo {
    magic: u32,
    width: u32,
    height: u32,
    format: u32,
    handle: u64,
    present_count: u64,
    copied_count: u64,
    hwnd: u64,
    pid: u32,
    _pad: u32,
}

#[derive(PartialEq, Clone, Copy)]
enum Source {
    Dda,
    Hook,
    Auto,
}

struct Args {
    w: u32,
    h: u32,
    secs: f64,
    fps: f64,
    source: Source,
    to_stdout: bool,
    cursor: bool,
    out: Option<String>,
    dumpframe: Option<String>,
    dumplast: Option<String>,
}

fn parse_args() -> Args {
    let mut a = Args {
        w: 1280, h: 720, secs: 0.0, fps: 180.0, source: Source::Dda,
        to_stdout: true, cursor: true, out: None, dumpframe: None, dumplast: None,
    };
    let argv: Vec<String> = std::env::args().collect();
    let mut i = 1;
    while i < argv.len() {
        match argv[i].as_str() {
            "--w" => { a.w = argv[i + 1].parse().unwrap(); i += 2; }
            "--h" => { a.h = argv[i + 1].parse().unwrap(); i += 2; }
            "--secs" => { a.secs = argv[i + 1].parse().unwrap(); i += 2; }
            "--fps" => { a.fps = argv[i + 1].parse().unwrap(); i += 2; }
            "--no-cursor" => { a.cursor = false; i += 1; }
            "--source" => {
                a.source = match argv[i + 1].as_str() {
                    "hook" => Source::Hook,
                    "auto" => Source::Auto,
                    _ => Source::Dda,
                };
                i += 2;
            }
            "--out" => { a.out = Some(argv[i + 1].clone()); a.to_stdout = false; i += 2; }
            "--dumpframe" => { a.dumpframe = Some(argv[i + 1].clone()); i += 2; }
            "--dumplast" => { a.dumplast = Some(argv[i + 1].clone()); i += 2; }
            "--stdout" => { a.to_stdout = true; i += 1; }
            _ => { i += 1; }
        }
    }
    a
}

macro_rules! elog {
    ($($t:tt)*) => {{ eprintln!($($t)*); let _ = std::io::stderr().flush(); }};
}

// A full GPU conversion path: input texture (RGB) -> NV12 output texture, scaled.
struct VpPath {
    vp: ID3D11VideoProcessor,
    in_view: ID3D11VideoProcessorInputView,
    out_view: ID3D11VideoProcessorOutputView,
}

unsafe fn build_vp(
    vdev: &ID3D11VideoDevice,
    in_tex: &ID3D11Texture2D, in_w: u32, in_h: u32,
    out_tex: &ID3D11Texture2D, out_w: u32, out_h: u32,
) -> Result<VpPath> {
    let content = D3D11_VIDEO_PROCESSOR_CONTENT_DESC {
        InputFrameFormat: D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE,
        InputFrameRate: DXGI_RATIONAL { Numerator: 180, Denominator: 1 },
        InputWidth: in_w, InputHeight: in_h,
        OutputFrameRate: DXGI_RATIONAL { Numerator: 180, Denominator: 1 },
        OutputWidth: out_w, OutputHeight: out_h,
        Usage: D3D11_VIDEO_USAGE_PLAYBACK_NORMAL,
    };
    let enumr = vdev.CreateVideoProcessorEnumerator(&content)?;
    let vp = vdev.CreateVideoProcessor(&enumr, 0)?;

    // Diagnostic: what format is the input, and does the VP advertise it?
    let mut idesc = D3D11_TEXTURE2D_DESC::default();
    in_tex.GetDesc(&mut idesc);
    let in_sup = enumr.CheckVideoProcessorFormat(idesc.Format).unwrap_or(0);
    let nv12_sup = enumr.CheckVideoProcessorFormat(DXGI_FORMAT_NV12).unwrap_or(0);
    elog!("build_vp: in_fmt={} (support=0x{:x}), nv12 support=0x{:x}",
        idesc.Format.0, in_sup, nv12_sup);

    let in_desc = D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC {
        FourCC: 0,
        ViewDimension: D3D11_VPIV_DIMENSION_TEXTURE2D,
        Anonymous: D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC_0 {
            Texture2D: D3D11_TEX2D_VPIV { MipSlice: 0, ArraySlice: 0 },
        },
    };
    let mut in_view: Option<ID3D11VideoProcessorInputView> = None;
    if let Err(e) = vdev.CreateVideoProcessorInputView(in_tex, &enumr, &in_desc, Some(&mut in_view)) {
        elog!("build_vp: CreateVideoProcessorInputView failed: {:?}", e);
        return Err(e);
    }

    let out_desc = D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC {
        ViewDimension: D3D11_VPOV_DIMENSION_TEXTURE2D,
        Anonymous: D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC_0 {
            Texture2D: D3D11_TEX2D_VPOV { MipSlice: 0 },
        },
    };
    let mut out_view: Option<ID3D11VideoProcessorOutputView> = None;
    vdev.CreateVideoProcessorOutputView(out_tex, &enumr, &out_desc, Some(&mut out_view))?;

    Ok(VpPath { vp, in_view: in_view.unwrap(), out_view: out_view.unwrap() })
}

unsafe fn vp_blt(vctx: &ID3D11VideoContext, path: &VpPath) -> Result<()> {
    let mut s = D3D11_VIDEO_PROCESSOR_STREAM::default();
    s.Enable = TRUE;
    s.pInputSurface = ManuallyDrop::new(Some(path.in_view.clone()));
    let r = vctx.VideoProcessorBlt(&path.vp, &path.out_view, 0, std::slice::from_ref(&s));
    let _ = ManuallyDrop::into_inner(s.pInputSurface); // release the per-call clone
    r
}

// Open the hook's shared texture on OUR device. Returns (info ptr, texture, w, h, fmt).
unsafe fn open_hook(
    device: &ID3D11Device, pid: u32,
) -> Option<(*mut SharedInfo, ID3D11Texture2D, u32, u32, DXGI_FORMAT)> {
    let name = format!("zentape_capture_info_{}\0", pid);
    let wide: Vec<u16> = name.encode_utf16().collect();
    let shm = OpenFileMappingW(FILE_MAP_ALL_ACCESS.0, false, PCWSTR(wide.as_ptr())).ok()?;
    let view = MapViewOfFile(shm, FILE_MAP_ALL_ACCESS, 0, 0, std::mem::size_of::<SharedInfo>());
    if view.Value.is_null() {
        return None;
    }
    let info = view.Value as *mut SharedInfo;
    if (*info).magic != 0x5A544341 || (*info).handle == 0 {
        return None;
    }
    let w = (*info).width;
    let h = (*info).height;
    let fmt = DXGI_FORMAT((*info).format as i32);
    let handle = (*info).handle;
    let mut tex: Option<ID3D11Texture2D> = None;
    if device
        .OpenSharedResource(HANDLE(handle as usize as *mut c_void), &mut tex)
        .is_err()
    {
        return None;
    }
    Some((info, tex.unwrap(), w, h, fmt))
}

unsafe fn is_cloaked(hwnd: HWND) -> bool {
    let mut val: u32 = 0;
    let _ = DwmGetWindowAttribute(
        hwnd, DWMWA_CLOAKED, &mut val as *mut u32 as *mut c_void, 4);
    val != 0
}

// NVIDIA/Discord/Steam/RTSS overlays are WS_EX_NOACTIVATE (can never become the
// foreground window) and usually WS_EX_TRANSPARENT (click-through). They aren't
// "an app on top of the game" -- ignore them in the occlusion walk. The real
// game is always the foreground window, which these never are.
unsafe fn is_overlay(hwnd: HWND) -> bool {
    let ex = GetWindowLongPtrW(hwnd, GWL_EXSTYLE) as u32;
    (ex & (WS_EX_TRANSPARENT.0 | WS_EX_NOACTIVATE.0)) != 0
}

fn rects_intersect(a: &RECT, b: &RECT) -> bool {
    a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top
}

fn covers(r: &RECT, mw: i32, mh: i32) -> bool {
    r.left <= 0 && r.top <= 0 && r.right >= mw && r.bottom >= mh
}

unsafe fn get_class_name(hwnd: HWND) -> String {
    let mut buf = [0u16; 256];
    let n = GetClassNameW(hwnd, &mut buf);
    String::from_utf16_lossy(&buf[..n.max(0) as usize])
}

// Does `game` cover the whole primary monitor with nothing meaningful drawn on
// top of it? This is the user's rule -- "just the game on screen" -> hook; if
// it isn't fullscreen or something is on top of it -> DDA. It's about what's
// VISIBLE (z-order + coverage), not keyboard focus: a borderless game covering
// the screen is "just the game" even if some unfocused window has focus.
unsafe fn window_is_sole_fullscreen(game: HWND, mw: i32, mh: i32, self_pid: u32) -> bool {
    if game.0.is_null() || !IsWindowVisible(game).as_bool() || is_cloaked(game) {
        return false;
    }
    // The game must be the active (foreground) app. Overlays are NOACTIVATE so
    // they never qualify; if the user tabs to another window, that window becomes
    // foreground (not the game) -> DDA.
    if GetForegroundWindow() != game {
        return false;
    }
    let mut gr = RECT::default();
    if GetWindowRect(game, &mut gr).is_err() || !covers(&gr, mw, mh) {
        return false;
    }
    let mon = RECT { left: 0, top: 0, right: mw, bottom: mh };
    let mut game_pid = 0u32;
    GetWindowThreadProcessId(game, Some(&mut game_pid));
    // Walk z-order from top; anything above `game` that's visible, not cloaked,
    // not the game's own/our own process, and covers >0.5% of the monitor is an
    // occluding overlay -> fall back to DDA.
    let mut w = GetTopWindow(None).unwrap_or(HWND(std::ptr::null_mut()));
    let mon_area = mw as i64 * mh as i64;
    while !w.0.is_null() {
        if w == game {
            break;
        }
        if IsWindowVisible(w).as_bool() && !is_cloaked(w) && !is_overlay(w) {
            let mut r = RECT::default();
            if GetWindowRect(w, &mut r).is_ok() && rects_intersect(&r, &mon) {
                let mut p = 0u32;
                GetWindowThreadProcessId(w, Some(&mut p));
                let area = (r.right - r.left) as i64 * (r.bottom - r.top) as i64;
                if p != game_pid && p != self_pid && area * 200 > mon_area {
                    return false;
                }
            }
        }
        w = GetWindow(w, GW_HWNDNEXT).unwrap_or(HWND(std::ptr::null_mut()));
    }
    true
}

// The candidate game = the foreground (active) window, when it covers the whole
// primary monitor and isn't the shell/an overlay/our own process. Using the
// foreground window auto-excludes NOACTIVATE overlays (NVIDIA/Discord/Steam).
unsafe fn find_game_window(mw: i32, mh: i32, self_pid: u32) -> Option<(HWND, u32, String)> {
    let fg = GetForegroundWindow();
    if fg.0.is_null() || !IsWindowVisible(fg).as_bool() || is_cloaked(fg) || is_overlay(fg) {
        return None;
    }
    let mut r = RECT::default();
    if GetWindowRect(fg, &mut r).is_err() || !covers(&r, mw, mh) {
        return None;
    }
    let mut pid = 0u32;
    GetWindowThreadProcessId(fg, Some(&mut pid));
    let cls = get_class_name(fg);
    if pid == 0 || pid == self_pid || cls == "Progman" || cls == "WorkerW" {
        return None;
    }
    Some((fg, pid, cls))
}

// Inject our hook DLL into `pid` (CreateRemoteThread(LoadLibraryW)).
unsafe fn inject(pid: u32, dll_wide: &[u16]) -> bool {
    let h = match OpenProcess(PROCESS_ALL_ACCESS, false, pid) {
        Ok(h) => h,
        Err(_) => return false,
    };
    let size = dll_wide.len() * 2;
    let remote = VirtualAllocEx(h, None, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    let mut ok = false;
    if !remote.is_null()
        && WriteProcessMemory(h, remote, dll_wide.as_ptr() as *const c_void, size, None).is_ok()
    {
        if let Ok(k32) = GetModuleHandleW(w!("kernel32.dll")) {
            let loadlib = GetProcAddress(k32, s!("LoadLibraryW"));
            let start: unsafe extern "system" fn(*mut c_void) -> u32 =
                std::mem::transmute(loadlib);
            if let Ok(thread) = CreateRemoteThread(h, None, 0, Some(start), Some(remote), 0, None) {
                WaitForSingleObject(thread, 5000);
                let _ = CloseHandle(thread);
                ok = true;
            }
        }
    }
    if !remote.is_null() {
        let _ = VirtualFreeEx(h, remote, 0, MEM_RELEASE);
    }
    let _ = CloseHandle(h);
    ok
}

// --- any-format -> B8G8R8A8 GPU pre-pass (for the hook path) ---
// The hook's backbuffer can be 8-bit (B8G8R8A8/R8G8B8A8), 10-bit (R10G10B10A2)
// or 16-bit float HDR (R16G16B16A16_FLOAT). The VideoProcessor only takes 8-bit
// RGB input on this hardware (R10G10B10A2 -> E_INVALIDARG), so a fullscreen pixel
// shader normalizes any source format into a B8G8R8A8 intermediate, which the
// existing VideoProcessor then scales+converts to NV12 exactly like the DDA path.
const CONVERT_HLSL: &str = r#"
Texture2D    tex : register(t0);
SamplerState smp : register(s0);
struct VSOut { float4 pos : SV_Position; float2 uv : TEXCOORD0; };
VSOut VSMain(uint id : SV_VertexID) {
    VSOut o;
    o.uv  = float2((id << 1) & 2, id & 2);
    o.pos = float4(o.uv.x * 2.0 - 1.0, 1.0 - o.uv.y * 2.0, 0.0, 1.0);
    return o;
}
float4 PSMain(VSOut i) : SV_Target {
    float3 c = tex.Sample(smp, i.uv).rgb;
    return float4(saturate(c), 1.0);
}
"#;

struct ConvertPass {
    vs: ID3D11VertexShader,
    ps: ID3D11PixelShader,
    sampler: ID3D11SamplerState,
}

struct HookConvert {
    src_srv: ID3D11ShaderResourceView, // SRV over the shared tex (its real fmt)
    inter: ID3D11Texture2D,            // B8G8R8A8_UNORM, RT|SR, full src size
    inter_rtv: ID3D11RenderTargetView,
}

unsafe fn compile_one(entry: PCSTR, target: PCSTR) -> Result<ID3DBlob> {
    let mut code: Option<ID3DBlob> = None;
    let mut errs: Option<ID3DBlob> = None;
    let hr = D3DCompile(
        CONVERT_HLSL.as_ptr() as *const c_void, CONVERT_HLSL.len(),
        s!("zentape_convert.hlsl"), None, None,
        entry, target, D3DCOMPILE_ENABLE_STRICTNESS, 0,
        &mut code, Some(&mut errs),
    );
    if let Err(e) = hr {
        if let Some(b) = errs {
            let msg = std::slice::from_raw_parts(
                b.GetBufferPointer() as *const u8, b.GetBufferSize());
            elog!("convert HLSL compile error:\n{}", String::from_utf8_lossy(msg));
        }
        return Err(e);
    }
    code.ok_or_else(|| Error::from(E_FAIL))
}

unsafe fn build_convert_pass(device: &ID3D11Device) -> Result<ConvertPass> {
    let vs_blob = compile_one(s!("VSMain"), s!("vs_5_0"))?;
    let ps_blob = compile_one(s!("PSMain"), s!("ps_5_0"))?;
    let vs_code = std::slice::from_raw_parts(
        vs_blob.GetBufferPointer() as *const u8, vs_blob.GetBufferSize());
    let ps_code = std::slice::from_raw_parts(
        ps_blob.GetBufferPointer() as *const u8, ps_blob.GetBufferSize());

    let mut vs: Option<ID3D11VertexShader> = None;
    device.CreateVertexShader(vs_code, None, Some(&mut vs))?;
    let mut ps: Option<ID3D11PixelShader> = None;
    device.CreatePixelShader(ps_code, None, Some(&mut ps))?;

    let samp_desc = D3D11_SAMPLER_DESC {
        Filter: D3D11_FILTER_MIN_MAG_MIP_LINEAR,
        AddressU: D3D11_TEXTURE_ADDRESS_CLAMP,
        AddressV: D3D11_TEXTURE_ADDRESS_CLAMP,
        AddressW: D3D11_TEXTURE_ADDRESS_CLAMP,
        MipLODBias: 0.0, MaxAnisotropy: 1,
        ComparisonFunc: D3D11_COMPARISON_NEVER,
        BorderColor: [0.0; 4], MinLOD: 0.0, MaxLOD: D3D11_FLOAT32_MAX,
    };
    let mut sampler: Option<ID3D11SamplerState> = None;
    device.CreateSamplerState(&samp_desc, Some(&mut sampler))?;

    Ok(ConvertPass {
        vs: vs.ok_or_else(|| Error::from(E_FAIL))?,
        ps: ps.ok_or_else(|| Error::from(E_FAIL))?,
        sampler: sampler.ok_or_else(|| Error::from(E_FAIL))?,
    })
}

unsafe fn build_hook_convert(
    device: &ID3D11Device, shared: &ID3D11Texture2D, src_fmt: DXGI_FORMAT, w: u32, h: u32,
) -> Result<HookConvert> {
    let srv_desc = D3D11_SHADER_RESOURCE_VIEW_DESC {
        Format: src_fmt,
        ViewDimension: D3D_SRV_DIMENSION_TEXTURE2D,
        Anonymous: D3D11_SHADER_RESOURCE_VIEW_DESC_0 {
            Texture2D: D3D11_TEX2D_SRV { MostDetailedMip: 0, MipLevels: 1 },
        },
    };
    let mut srv: Option<ID3D11ShaderResourceView> = None;
    device.CreateShaderResourceView(shared, Some(&srv_desc), Some(&mut srv))?;

    let inter_desc = D3D11_TEXTURE2D_DESC {
        Width: w, Height: h, MipLevels: 1, ArraySize: 1,
        Format: DXGI_FORMAT_B8G8R8A8_UNORM,
        SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
        Usage: D3D11_USAGE_DEFAULT,
        BindFlags: (D3D11_BIND_RENDER_TARGET.0 | D3D11_BIND_SHADER_RESOURCE.0) as u32,
        CPUAccessFlags: 0, MiscFlags: 0,
    };
    let mut inter: Option<ID3D11Texture2D> = None;
    device.CreateTexture2D(&inter_desc, None, Some(&mut inter))?;
    let inter = inter.unwrap();

    let rtv_desc = D3D11_RENDER_TARGET_VIEW_DESC {
        Format: DXGI_FORMAT_B8G8R8A8_UNORM,
        ViewDimension: D3D11_RTV_DIMENSION_TEXTURE2D,
        Anonymous: D3D11_RENDER_TARGET_VIEW_DESC_0 {
            Texture2D: D3D11_TEX2D_RTV { MipSlice: 0 },
        },
    };
    let mut rtv: Option<ID3D11RenderTargetView> = None;
    device.CreateRenderTargetView(&inter, Some(&rtv_desc), Some(&mut rtv))?;

    Ok(HookConvert {
        src_srv: srv.ok_or_else(|| Error::from(E_FAIL))?,
        inter,
        inter_rtv: rtv.ok_or_else(|| Error::from(E_FAIL))?,
    })
}

// any-format SRV -> B8G8R8A8 intermediate, then unbind so it can be a VP input.
unsafe fn convert_blt(ctx: &ID3D11DeviceContext, cp: &ConvertPass, hc: &HookConvert, w: u32, h: u32) {
    ctx.IASetInputLayout(None);
    ctx.IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx.VSSetShader(&cp.vs, None);
    ctx.PSSetShader(&cp.ps, None);
    ctx.PSSetShaderResources(0, Some(&[Some(hc.src_srv.clone())]));
    ctx.PSSetSamplers(0, Some(&[Some(cp.sampler.clone())]));
    let vp = D3D11_VIEWPORT {
        TopLeftX: 0.0, TopLeftY: 0.0, Width: w as f32, Height: h as f32,
        MinDepth: 0.0, MaxDepth: 1.0,
    };
    ctx.RSSetViewports(Some(&[vp]));
    ctx.OMSetRenderTargets(Some(&[Some(hc.inter_rtv.clone())]), None);
    ctx.Draw(3, 0);
    // unbind (critical): RTV so `inter` can be a VP input; SRV so the shared tex
    // isn't left bound when the hook overwrites it next frame.
    ctx.OMSetRenderTargets(Some(&[None]), None);
    ctx.PSSetShaderResources(0, Some(&[None]));
}

// ---- OS cursor draw (DDA path) ----
// Draw the live OS cursor straight onto the captured BGRA frame via GDI before
// downscale. DrawIconEx does native mask/XOR blending, so inverting cursors (the
// I-beam over text) come out correct -- the same Win32 call the Python path used.
// The BGRA texture is created GDI-compatible so IDXGISurface1::GetDC works.
unsafe fn cursor_hotspot(hcur: HCURSOR) -> (i32, i32) {
    let mut ii = ICONINFO::default();
    if GetIconInfo(HICON(hcur.0), &mut ii).is_ok() {
        let hs = (ii.xHotspot as i32, ii.yHotspot as i32);
        if !ii.hbmColor.is_invalid() {
            let _ = DeleteObject(HGDIOBJ(ii.hbmColor.0));
        }
        if !ii.hbmMask.is_invalid() {
            let _ = DeleteObject(HGDIOBJ(ii.hbmMask.0));
        }
        hs
    } else {
        (0, 0)
    }
}

unsafe fn draw_cursor(bgra: &ID3D11Texture2D, cache: &mut (usize, (i32, i32))) {
    let mut ci = CURSORINFO {
        cbSize: std::mem::size_of::<CURSORINFO>() as u32,
        ..Default::default()
    };
    if GetCursorInfo(&mut ci).is_err() || ci.flags.0 != CURSOR_SHOWING.0 {
        return;
    }
    let hcur = ci.hCursor;
    if (hcur.0 as usize) == 0 {
        return;
    }
    let key = hcur.0 as usize;
    let hot = if cache.0 == key {
        cache.1
    } else {
        let h = cursor_hotspot(hcur);
        *cache = (key, h);
        h
    };
    let x = ci.ptScreenPos.x - hot.0;
    let y = ci.ptScreenPos.y - hot.1;
    if let Ok(surface) = bgra.cast::<IDXGISurface1>() {
        if let Ok(hdc) = surface.GetDC(false) {
            let _ = DrawIconEx(hdc, x, y, HICON(hcur.0), 0, 0, 0, None, DI_NORMAL);
            let _ = surface.ReleaseDC(None);
        }
    }
}

fn main() -> Result<()> {
    let args = parse_args();
    let (dst_w, dst_h) = (args.w, args.h);
    let want = match args.source {
        Source::Dda => "dda",
        Source::Hook => "hook",
        Source::Auto => "auto",
    };
    elog!("engine: target {}x{} nv12, secs={}, source={}", dst_w, dst_h, args.secs, want);

    unsafe {
        // Physical-pixel coordinates for cursor position + window rects.
        let _ = SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);

        // --- D3D11 device (BGRA + video support) ---
        let mut device: Option<ID3D11Device> = None;
        let mut context: Option<ID3D11DeviceContext> = None;
        let flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT | D3D11_CREATE_DEVICE_VIDEO_SUPPORT;
        D3D11CreateDevice(
            None, D3D_DRIVER_TYPE_HARDWARE, HMODULE::default(), flags, None,
            D3D11_SDK_VERSION, Some(&mut device), None, Some(&mut context),
        )?;
        let device = device.unwrap();
        let context = context.unwrap();
        let vdev: ID3D11VideoDevice = device.cast()?;
        let vctx: ID3D11VideoContext = context.cast()?;
        let convert = build_convert_pass(&device)?;

        // --- NV12 output target (+ staging for readback) ---
        let nv12_desc = D3D11_TEXTURE2D_DESC {
            Width: dst_w, Height: dst_h, MipLevels: 1, ArraySize: 1,
            Format: DXGI_FORMAT_NV12,
            SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
            Usage: D3D11_USAGE_DEFAULT,
            BindFlags: D3D11_BIND_RENDER_TARGET.0 as u32,
            CPUAccessFlags: 0, MiscFlags: 0,
        };
        let mut nv12: Option<ID3D11Texture2D> = None;
        device.CreateTexture2D(&nv12_desc, None, Some(&mut nv12))?;
        let nv12 = nv12.unwrap();
        let stage_desc = D3D11_TEXTURE2D_DESC {
            Usage: D3D11_USAGE_STAGING, BindFlags: 0,
            CPUAccessFlags: D3D11_CPU_ACCESS_READ.0 as u32, ..nv12_desc
        };
        let mut stage: Option<ID3D11Texture2D> = None;
        device.CreateTexture2D(&stage_desc, None, Some(&mut stage))?;
        let stage = stage.unwrap();

        // --- DDA setup (always available) ---
        let dxgi_dev: IDXGIDevice = device.cast()?;
        let adapter: IDXGIAdapter = dxgi_dev.GetAdapter()?;
        let output: IDXGIOutput = adapter.EnumOutputs(0)?;
        let output1: IDXGIOutput1 = output.cast()?;
        let mut dup: IDXGIOutputDuplication = output1.DuplicateOutput(&device)?;
        let dup_desc = dup.GetDesc();
        let src_w = dup_desc.ModeDesc.Width;
        let src_h = dup_desc.ModeDesc.Height;
        elog!("engine: DDA {}x{} fmt={}", src_w, src_h, dup_desc.ModeDesc.Format.0);

        let bgra_desc = D3D11_TEXTURE2D_DESC {
            Width: src_w, Height: src_h, MipLevels: 1, ArraySize: 1,
            Format: DXGI_FORMAT_B8G8R8A8_UNORM,
            SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
            Usage: D3D11_USAGE_DEFAULT,
            BindFlags: (D3D11_BIND_RENDER_TARGET.0 | D3D11_BIND_SHADER_RESOURCE.0) as u32,
            CPUAccessFlags: 0,
            MiscFlags: if args.cursor { D3D11_RESOURCE_MISC_GDI_COMPATIBLE.0 as u32 } else { 0 },
        };
        let mut bgra: Option<ID3D11Texture2D> = None;
        device.CreateTexture2D(&bgra_desc, None, Some(&mut bgra))?;
        let bgra = bgra.unwrap();
        let dda_path = build_vp(&vdev, &bgra, src_w, src_h, &nv12, dst_w, dst_h)?;

        // --- HOOK setup (lazy: wait for the SHM/shared texture to appear) ---
        // (info, shared tex, any-fmt->BGRA convert pass, VP path on the BGRA inter, last_copied)
        let mut hook: Option<(*mut SharedInfo, ID3D11Texture2D, HookConvert, VpPath, u64)> = None;

        // --- output sink ---
        let mut file_sink = args.out.as_ref().map(|p| std::fs::File::create(p).unwrap());
        let stdout = std::io::stdout();
        let mut so = stdout.lock();
        let frame_bytes = (dst_w as usize * dst_h as usize) * 3 / 2;
        let mut buf = vec![0u8; frame_bytes];
        let mut dumped = args.dumpframe.is_none();

        let start = Instant::now();
        let mut sec_mark = Instant::now();
        let (mut emitted, mut sec_emit) = (0u64, 0u64);
        let mut sec_hook = 0u64;
        let mut sec_dda = 0u64;

        // Phase 4 auto-switch state.
        let self_pid = std::process::id();
        let dll_path = std::env::current_exe().ok()
            .and_then(|p| p.parent().map(|d| d.join("zentape_hook.dll")))
            .map(|p| p.to_string_lossy().to_string())
            .unwrap_or_default();
        let dll_wide: Vec<u16> = dll_path.encode_utf16().chain(std::iter::once(0)).collect();
        let (mw, mh) = (src_w as i32, src_h as i32);
        let mut auto_use_hook = false;
        let mut eval_mark = Instant::now();
        let mut eval_copied = 0u64;
        let mut injected: Vec<u32> = Vec::new();
        let mut target_pid: Option<u32> = None;
        let mut stall = 0u32; // consecutive evals with no new hook frame

        // Output pacing: emit a steady `fps` frames/sec in real time. Capture runs
        // as fast as it can into the NV12 target (marking it dirty); the emit step
        // writes one frame per real-time slot (reading back only when there's new
        // content, duplicating otherwise). Even, paced writes -> ffmpeg reads
        // evenly -> its CFR step stops dropping the bursts a free-running emit made.
        let period = 1.0 / args.fps.max(1.0);
        let mut nv12_valid = false; // NV12 target has at least one captured frame
        let mut nv12_dirty = false; // new content captured since last readback
        let mut next_emit_t = 0.0f64; // wall-clock time of the next output slot
        let mut started = false;
        let mut done = false;
        let mut cursor_cache: (usize, (i32, i32)) = (0, (0, 0)); // hcursor -> hotspot

        loop {
            if args.secs > 0.0 && start.elapsed().as_secs_f64() >= args.secs {
                break;
            }

            // Every 100ms (non-DDA modes): pick the on-screen fullscreen game,
            // inject + attach its hook, then decide hook-vs-DDA. Sharing ONE NV12
            // output makes the switch seamless downstream.
            if args.source != Source::Dda && eval_mark.elapsed().as_millis() >= 100 {
                eval_mark = Instant::now();

                // (1) Pick a target game (topmost fullscreen, non-overlay) + inject.
                if target_pid.is_none() && !dll_wide.is_empty() {
                    if let Some((_h, pid, cls)) = find_game_window(mw, mh, self_pid) {
                        target_pid = Some(pid);
                        if !injected.contains(&pid) {
                            injected.push(pid);
                            let ok = inject(pid, &dll_wide);
                            elog!("engine: target pid {} (class {}) inject -> {}", pid, cls, ok);
                        }
                    }
                }

                // (2) Attach the hook (open the target's per-pid shared texture).
                if hook.is_none() {
                    if let Some(pid) = target_pid {
                        if let Some((info, tex, hw, hh, hfmt)) = open_hook(&device, pid) {
                            match build_hook_convert(&device, &tex, hfmt, hw, hh) {
                                Ok(hc) => match build_vp(&vdev, &hc.inter, hw, hh, &nv12, dst_w, dst_h) {
                                    Ok(hp) => {
                                        elog!("engine: hook attached pid {} {}x{} fmt={} (via convert pass)",
                                            pid, hw, hh, hfmt.0);
                                        hook = Some((info, tex, hc, hp, 0));
                                        eval_copied = 0;
                                        stall = 0;
                                    }
                                    Err(e) => elog!("engine: hook build_vp failed: {:?}", e),
                                },
                                Err(e) => elog!("engine: hook build_convert failed: {:?}", e),
                            }
                        }
                    }
                }

                // (3) Decide, and drop a dead target so a new game can be picked up.
                if let Some((info, _, _, _, _)) = hook.as_ref() {
                    let info = *info;
                    let copied = std::ptr::read_volatile(std::ptr::addr_of!((*info).copied_count));
                    let alive = copied != eval_copied;
                    eval_copied = copied;
                    stall = if alive { 0 } else { stall + 1 };
                    let gh = HWND(
                        std::ptr::read_volatile(std::ptr::addr_of!((*info).hwnd)) as usize
                            as *mut c_void,
                    );
                    if stall >= 5 {
                        // ~500ms with no new frame: game closed/minimized -> reset.
                        elog!("engine: hook target idle, releasing");
                        hook = None;
                        target_pid = None;
                        eval_copied = 0;
                        stall = 0;
                        if auto_use_hook {
                            auto_use_hook = false;
                            elog!("engine: SWITCH source -> dda");
                        }
                    } else {
                        let new_use = match args.source {
                            Source::Hook => alive, // forced: hook whenever frames flow
                            _ => alive && window_is_sole_fullscreen(gh, mw, mh, self_pid),
                        };
                        if new_use != auto_use_hook {
                            elog!("engine: SWITCH source -> {}",
                                if new_use { "hook" } else { "dda" });
                            auto_use_hook = new_use;
                        }
                    }
                } else if auto_use_hook {
                    auto_use_hook = false;
                    elog!("engine: SWITCH source -> dda");
                }
            }

            // Decide source for this frame (auto_use_hook stays false in DDA mode).
            let use_hook = auto_use_hook;

            // CAPTURE: pull the newest source frame into the NV12 target and mark
            // it dirty. No emit here -- emission is paced below so writes are even.
            if use_hook {
                let (info, _tex, hc, hp, last_copied) = hook.as_mut().unwrap();
                let copied = std::ptr::read_volatile(&(**info).copied_count);
                if copied != *last_copied {
                    *last_copied = copied;
                    let mut d = D3D11_TEXTURE2D_DESC::default();
                    hc.inter.GetDesc(&mut d);
                    convert_blt(&context, &convert, hc, d.Width, d.Height); // any-fmt -> BGRA
                    vp_blt(&vctx, hp)?; // BGRA inter -> NV12 (scale)
                    nv12_valid = true;
                    nv12_dirty = true;
                    sec_hook += 1;
                }
            } else {
                let mut fi = DXGI_OUTDUPL_FRAME_INFO::default();
                let mut res: Option<IDXGIResource> = None;
                match dup.AcquireNextFrame(2, &mut fi, &mut res) {
                    Ok(_) => {
                        if let Some(res) = res.as_ref() {
                            if let Ok(frame_tex) = res.cast::<ID3D11Texture2D>() {
                                context.CopyResource(&bgra, &frame_tex);
                            }
                        }
                        let _ = dup.ReleaseFrame();
                        if args.cursor {
                            draw_cursor(&bgra, &mut cursor_cache); // OS cursor onto BGRA
                        }
                        vp_blt(&vctx, &dda_path)?;
                        nv12_valid = true;
                        nv12_dirty = true;
                        sec_dda += 1;
                    }
                    Err(e) if e.code() == DXGI_ERROR_WAIT_TIMEOUT => {}
                    Err(_e) => {
                        // ACCESS_LOST etc -- a game took TRUE exclusive fullscreen
                        // (DDA can't see FSE). Don't die: recreate the duplication;
                        // if that fails (FSE active), skip and let the hook cover it.
                        match output1.DuplicateOutput(&device) {
                            Ok(d) => dup = d,
                            Err(_) => std::thread::sleep(std::time::Duration::from_millis(40)),
                        }
                    }
                }
            }

            // EMIT (capture-aligned pacing): hold a steady `fps` output, but emit
            // each fresh capture as it arrives (up to one slot early) so unique
            // frames aren't lost to capture/slot phase misalignment; duplicate the
            // last frame to fill slots when the source is slower than `fps`.
            let now = start.elapsed().as_secs_f64();
            if nv12_valid && !started {
                next_emit_t = now;
                started = true;
            }
            while nv12_valid {
                let slot_due = now >= next_emit_t;
                if nv12_dirty {
                    // Don't run more than ~one period ahead of real time.
                    if !slot_due && (now + period) < next_emit_t {
                        break;
                    }
                    context.CopyResource(&stage, &nv12);
                    let mut m = D3D11_MAPPED_SUBRESOURCE::default();
                    if context.Map(&stage, 0, D3D11_MAP_READ, 0, Some(&mut m)).is_ok() {
                        let base = m.pData as *const u8;
                        let pitch = m.RowPitch as usize;
                        let (w, h) = (dst_w as usize, dst_h as usize);
                        for row in 0..h {
                            std::ptr::copy_nonoverlapping(
                                base.add(row * pitch), buf.as_mut_ptr().add(row * w), w);
                        }
                        let (uv_dst0, uv_src0) = (w * h, pitch * h);
                        for row in 0..(h / 2) {
                            std::ptr::copy_nonoverlapping(
                                base.add(uv_src0 + row * pitch),
                                buf.as_mut_ptr().add(uv_dst0 + row * w), w);
                        }
                        context.Unmap(&stage, 0);
                        nv12_dirty = false;
                        if !dumped {
                            let p = args.dumpframe.as_ref().unwrap();
                            std::fs::write(p, &buf).unwrap();
                            elog!("engine: dumped one NV12 frame to {} ({} bytes)", p, buf.len());
                            dumped = true;
                        }
                    } else {
                        break;
                    }
                } else if !slot_due {
                    // no fresh content and the slot isn't due -> wait
                    break;
                }
                if let Some(f) = file_sink.as_mut() {
                    f.write_all(&buf).unwrap();
                } else if args.to_stdout && so.write_all(&buf).is_err() {
                    done = true;
                    break;
                }
                emitted += 1;
                sec_emit += 1;
                next_emit_t += period;
                if next_emit_t < now - 0.25 {
                    next_emit_t = now; // anti-spiral: resync if far behind
                }
            }
            if done {
                break;
            }
            // Idle nap (hook path) so we don't busy-spin between slots; the DDA
            // path already waits up to 2ms inside AcquireNextFrame.
            if use_hook && nv12_valid && !nv12_dirty {
                let now2 = start.elapsed().as_secs_f64();
                if next_emit_t > now2 {
                    let nap_us = (((next_emit_t - now2) * 1_000_000.0) as u64).min(1500);
                    if nap_us > 0 {
                        std::thread::sleep(std::time::Duration::from_micros(nap_us));
                    }
                }
            }

            if sec_mark.elapsed().as_secs_f64() >= 1.0 {
                let dt = sec_mark.elapsed().as_secs_f64();
                elog!("engine: emit {:.0}/s  (dda {:.0} hook {:.0})  src={}",
                    sec_emit as f64 / dt, sec_dda as f64 / dt, sec_hook as f64 / dt,
                    if use_hook { "hook" } else { "dda" });
                sec_emit = 0; sec_dda = 0; sec_hook = 0;
                sec_mark = Instant::now();
            }
        }

        if args.to_stdout { let _ = so.flush(); }
        if let Some(f) = file_sink.as_mut() { let _ = f.flush(); }
        if let Some(p) = args.dumplast.as_ref() {
            if emitted > 0 {
                std::fs::write(p, &buf).ok();
                elog!("engine: dumped last frame to {}", p);
            }
        }
        let total = start.elapsed().as_secs_f64();
        elog!("engine: done. emitted {} ({:.1}/s)", emitted, emitted as f64 / total);
    }
    Ok(())
}
