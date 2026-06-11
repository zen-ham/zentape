// Inject zentape_hook.dll into a target process (by exe name) via the classic
// CreateRemoteThread(LoadLibraryW) technique.
use std::ffi::c_void;
use std::mem::size_of;
use windows::core::*;
use windows::Win32::Foundation::*;
use windows::Win32::System::Diagnostics::Debug::WriteProcessMemory;
use windows::Win32::System::Diagnostics::ToolHelp::*;
use windows::Win32::System::LibraryLoader::*;
use windows::Win32::System::Memory::*;
use windows::Win32::System::Threading::*;

fn find_pid(name: &str) -> Option<u32> {
    unsafe {
        let snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0).ok()?;
        let mut e = PROCESSENTRY32W {
            dwSize: size_of::<PROCESSENTRY32W>() as u32,
            ..Default::default()
        };
        let mut found = None;
        if Process32FirstW(snap, &mut e).is_ok() {
            loop {
                let exe = String::from_utf16_lossy(&e.szExeFile);
                let exe = exe.trim_end_matches('\0');
                if exe.eq_ignore_ascii_case(name) {
                    found = Some(e.th32ProcessID);
                    break;
                }
                if Process32NextW(snap, &mut e).is_err() {
                    break;
                }
            }
        }
        let _ = CloseHandle(snap);
        found
    }
}

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let target = args.get(1).map(|s| s.as_str()).unwrap_or("flip_demo.exe");
    let exe = std::env::current_exe().unwrap();
    let dll = exe.parent().unwrap().join("zentape_hook.dll");
    let dll_str = dll.to_string_lossy().to_string();
    println!("injecting {} -> {}", dll_str, target);

    let pid = match find_pid(target) {
        Some(p) => p,
        None => {
            eprintln!("process '{}' not found", target);
            std::process::exit(1);
        }
    };
    println!("found pid {}", pid);

    unsafe {
        let h = OpenProcess(PROCESS_ALL_ACCESS, false, pid)?;
        let wpath: Vec<u16> = dll_str.encode_utf16().chain(std::iter::once(0)).collect();
        let size = wpath.len() * 2;
        let remote = VirtualAllocEx(h, None, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
        if remote.is_null() {
            eprintln!("VirtualAllocEx failed");
            std::process::exit(1);
        }
        WriteProcessMemory(h, remote, wpath.as_ptr() as *const c_void, size, None)?;
        let kernel32 = GetModuleHandleW(w!("kernel32.dll"))?;
        let loadlib = GetProcAddress(kernel32, s!("LoadLibraryW"));
        let start: unsafe extern "system" fn(*mut c_void) -> u32 =
            std::mem::transmute(loadlib);
        let thread = CreateRemoteThread(h, None, 0, Some(start), Some(remote), 0, None)?;
        WaitForSingleObject(thread, 10000);
        let _ = VirtualFreeEx(h, remote, 0, MEM_RELEASE);
        let _ = CloseHandle(thread);
        let _ = CloseHandle(h);
    }
    println!("injected. check %TEMP%\\zentape_hook.log");
    Ok(())
}
