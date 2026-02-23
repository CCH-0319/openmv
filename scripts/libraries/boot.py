# main.py (OpenMV / MicroPython for OpenMV4 H7 Plus)

# ===== Import =====
import os, time, pyb, sensor
import ustruct as struct
from pyb import Pin
import gc

# ===== Version =====
FIRMWARE_VERSION = "1.0.0"

# ===== GPIO =====
led_white  = Pin('PC13', Pin.OUT_PP)
gpio_led   = Pin('PB14', Pin.OUT_PP)
gpio_ir    = Pin('PB13', Pin.OUT_PP)
gpio_cut_p = Pin('PB12', Pin.OUT_PP)
gpio_cut_n = Pin('PD8', Pin.OUT_PP)
led_en = 0
ir_en  = 0
ir_cut = 0

# ===== USB =====
usb = pyb.USB_VCP()

# ===== UART =====
UART_ID = 1
ALLOWED_BAUDS = (9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600)
UART_BAUD = 115200
uart = pyb.UART(UART_ID, UART_BAUD, timeout_char=50)

def uart_init(baud):
    global uart, UART_BAUD
    try:
        uart.init(baudrate=int(baud), bits=8, parity=None, stop=1, timeout_char=50)
        UART_BAUD = int(baud)
        return True
    except Exception:
        return False

# ===== RTC =====
rtc = pyb.RTC()

# ===== Camera defaults =====
RESOLUTION_MAP = {
    'QVGA': getattr(sensor, 'QVGA', sensor.QVGA),
    'VGA':  getattr(sensor, 'VGA',  sensor.VGA),
    'SVGA': getattr(sensor, 'SVGA', sensor.SVGA),
    'XGA':  getattr(sensor, 'XGA',  sensor.XGA),
    'SXGA': getattr(sensor, 'SXGA', sensor.SXGA),
    'UXGA': getattr(sensor, 'UXGA', sensor.UXGA),
    'HD':   getattr(sensor, 'HD',   getattr(sensor, 'UXGA', sensor.VGA)),
    'FHD':  getattr(sensor, 'FHD',  getattr(sensor, 'UXGA', sensor.VGA)),
}

RESO_NAME = {}
for k, v in RESOLUTION_MAP.items():
    RESO_NAME[v] = k

frame_size        = RESOLUTION_MAP['VGA']
skip_ms           = 200         # 100..5000
expo_time_us      = 0           # 0=AUTO; 手動 100..100000 us
expo_auto         = True
contrast_level    = 0           # -3..+3
brightness_level  = 0           # -3..+3
saturation_level  = 0           # -3..+3
gain_auto         = True        # Lock AUTO
wbal_auto         = True        # Lock AUTO

# ===== JPEG buffer (bytes) =====
jpeg = None
JPEG_FOLDER  = "JPEG"

# ===== Config file =====
#CONFIG_PATH  = "/flash/config.ini"
CONFIG_PATH  = "/config.ini"

# ===== SD CARD hot-plug helpers (支持 /sdcard 與 /sd) =====
_sd_dev = None
sd_busy_until = 0  # ticks_ms 截止時間：期間內視為 SD 正忙碌，避免卸載
_SD_MOUNT_CANDIDATES = ('/sdcard', '/sd')
_sd_mount_path = None  # 真正掛載的路徑(/sdcard 或 /sd)

def sd_mark_busy(ms=500):   # 在未來 ms 毫秒內視為 SD 正忙(寫入/flush/建目錄/掛載等)
    global sd_busy_until
    sd_busy_until = time.ticks_add(time.ticks_ms(), ms)

def sd_is_busy():
    return (time.ticks_diff(sd_busy_until, time.ticks_ms()) > 0)

def _root_entries():
    try:
        return set(os.listdir('/'))
    except Exception:
        return set()

def sd_is_mounted():    # 是否已在 /sdcard 或 /sd 掛載(同時更新 _sd_mount_path)
    global _sd_mount_path
    roots = _root_entries()
    for p in _SD_MOUNT_CANDIDATES:
        name = p[1:]  # '/sdcard' -> 'sdcard'
        if name in roots:
            _sd_mount_path = p
            return True
    return False

def sd_mount_path():    # 回傳目前/預計的掛載點：優先 /sdcard，其次 /sd
    if sd_is_mounted():
        return _sd_mount_path
    return _SD_MOUNT_CANDIDATES[0]

def sd_present():       # 偵測卡是否存在：有 present() 就用；否則嘗試 init()
    global _sd_dev
    try:
        if _sd_dev is None:
            _sd_dev = pyb.SDCard()
        if hasattr(_sd_dev, 'present'):
            return bool(_sd_dev.present())
        try:
            _sd_dev.init()
            return True
        except Exception:
            print("_sd_dev.init")
            return False
    except Exception:
        return False

def sd_try_mount():     # 卡在、未掛 → 掛上(優先 /sdcard)；成功回 True
    global _sd_dev, _sd_mount_path
    try:
        if sd_is_mounted(): return True
        if not sd_present(): return False
        if _sd_dev is None: _sd_dev = pyb.SDCard()
        mount_at = sd_mount_path()
        os.mount(_sd_dev, mount_at)
        _sd_mount_path = mount_at
        sd_mark_busy(200)
        return True
    except Exception:
        return False

def sd_try_unmount(force=False):    # 卡拔掉或要卸載 → umount；忙碌時若非 force 則延後
    global _sd_mount_path
    try:
        if not sd_is_mounted(): return True
        if (not force) and sd_is_busy(): return False
        os.umount(_sd_mount_path)
        _sd_mount_path = None
        return True
    except Exception:
        return False

# ===== SD CARD high level =====
def check_sd_card(folder=JPEG_FOLDER):  # 確保卡已掛載且資料夾存在
    try:
        if not sd_is_mounted():
            if not sd_try_mount():
                return False
            sd_mark_busy(200)
        base = sd_mount_path()
        items = set(os.listdir(base))
        if folder not in items:
            os.mkdir("{}/{}".format(base, folder))
            sd_mark_busy(200)
        return True
    except Exception:
        return False

def save_jpeg_to_sd(jpg_bytes, fname=None, folder=JPEG_FOLDER):
    if not check_sd_card(folder):
        send_line("$CARD=NOT_READY")
        return False
    base = sd_mount_path()
    if fname is None:
        y,m,d,wd,hh,mm,ss,sub = rtc.datetime()
        fname = "%04d%02d%02d_%02d%02d%02d.jpg" % (y,m,d,hh,mm,ss)
    fpath = "{}/{}/{}".format(base, folder, fname)
    sd_mark_busy(1000)
    try:
        with open(fpath, "wb") as f:
            f.write(jpg_bytes)
            f.flush()
        try:
            os.sync()
        finally:
            sd_mark_busy(1000)
        return True
    except Exception:
        send_line("$CARD=WRITE_FAILED")
        return False

# ===== 通訊抽象層：USB 優先，否則 UART =====
def link_is_usb():
    try: return bool(usb and usb.isconnected())
    except Exception: return False

def link_write_once(buf, timeout=5000):     # 允許 bytes / bytearray / memoryview
    try:
        if link_is_usb():
            try:
                sent = usb.send(buf, timeout=timeout)
                if sent is None: sent = len(buf)
                return sent
            except AttributeError:
                w = usb.write(buf); return w if w else 0
        else:
            w = uart.write(buf); return w if w else 0
    except TypeError:
        b = bytes(buf)
        if link_is_usb():
            try:
                sent = usb.send(b, timeout=timeout)
                if sent is None: sent = len(b)
                return sent
            except AttributeError:
                w = usb.write(b); return w if w else 0
        else:
            w = uart.write(b); return w if w else 0

def link_send_all(buf, timeout=5000):
    try:
        mv = buf if isinstance(buf, memoryview) else memoryview(buf)
    except TypeError:
        mv = memoryview(bytes(buf))
    off = 0; ln = len(mv)
    while off < ln:
        sent = link_write_once(mv[off:], timeout=timeout)
        if not sent:
            pyb.udelay(200)
            continue
        off += sent
    return off

def send_line(text):
    data = (text + "\r\n").encode()
    try: link_send_all(data)
    except Exception: pass

# ===== bytes 安全工具 =====
def _emit(msg):
    send_line(msg)

def _ascii_clean(b):
    out = bytearray()
    for x in b:
        if 32 <= x <= 126 or x in (9, 10, 13):
            out.append(x)
    return out.decode()

# ===== Config 存取 =====
def save_config():
    try:
        cfg = []
        # HEADER
        cfg.append("# CCH Config")
        y,m,d,wd,hh,mm,ss,sub = rtc.datetime()
        cfg.append("# Saved: " + ("%04d-%02d-%02d %02d:%02d:%02d" % (y,m,d,hh,mm,ss)))
        # CAMERA
        cfg.append("")
        cfg.append("[CAMERA]")
        # resolution
        cfg.append("RESO=" + RESO_NAME.get(frame_size, "VGA"))
        # frames skiped
        cfg.append("SKIP=" + str(int(skip_ms)))
        # expo
        cfg.append("EXPO=" + ("AUTO" if expo_auto else str(int(expo_time_us))))
        # contrast
        cfg.append("CTST=" + str(int(contrast_level)))
        # brightness
        cfg.append("BRIT=" + str(int(brightness_level)))
        # saturation
        cfg.append("SATR=" + str(int(saturation_level)))
        # gain auto
        cfg.append("GAIN=" + "AUTO")
        # white balance auto
        cfg.append("WBAL=" + "AUTO")
        # led
        cfg.append("LTEN=" + str(int(led_en)))
        # ir
        cfg.append("IREN=" + str(int(ir_en)))
        # ir cut
        cfg.append("IRCT=" + str(int(ir_cut)))
        # LINK
        cfg.append("")
        cfg.append("[LINK]")
        # serial baudrate
        cfg.append("BAUD=" + str(int(UART_BAUD)))
        cfg.append("")
        # 寫入組態檔
        with open(CONFIG_PATH, "w") as f:
            f.write("\n".join(cfg));
            f.flush()
        try:
            os.sync()
        except Exception:
            pass
        return True
    except Exception:
        return False

def load_config():
    global frame_size, skip_ms, expo_time_us, expo_auto
    global contrast_level, brightness_level, saturation_level
    global gain_auto, wbal_auto
    global led_en, ir_en, ir_cut
    global UART_BAUD
    try:
        with open(CONFIG_PATH, "r") as f:
            section = None
            for raw in f:
                s = raw.strip()
                if not s or s.startswith("#") or s.startswith(";"): continue
                if s.startswith("[") and s.endswith("]"):
                    section = s[1:-1].strip().upper(); continue
                s = s.split("#", 1)[0].split(";", 1)[0].strip()
                if not s or "=" not in s: continue
                key, val = s.split("=", 1)
                key = key.strip().upper(); val = val.strip()
                if section in (None, "CAMERA"):
                    # resolution
                    if key == "RESO":
                        k = val.upper()
                        if k in RESOLUTION_MAP: frame_size = RESOLUTION_MAP[k]
                    # frames skiped
                    elif key == "SKIP":
                        try:
                            ms = int(val)
                            if 0 <= ms <= 10000: skip_ms = ms
                        except: skip_ms = 500
                    # expo
                    elif key == "EXPO":
                        v = val.upper()
                        if v == "AUTO":
                            expo_auto = True; expo_time_us = 0
                        else:
                            try:
                                us = int(v)
                                if 100 <= us <= 100000:
                                    expo_auto = False; expo_time_us = us
                            except: expo_auto = True; expo_time_us = 0
                    # contrast
                    elif key == "CTST":
                        try:
                            c = int(val)
                            if -3 <= c <= 3: contrast_level = c
                        except: contrast_level = 0
                    # brightness
                    elif key == "BRIT":
                        try:
                            b = int(val)
                            if -3 <= b <= 3: brightness_level = b
                        except: brightness_level = 0
                    # stauration
                    elif key == "SATR":
                        try:
                            t = int(val)
                            if -3 <= t <= 3: saturation_level = t
                        except: saturation_level = 0
                    # gain auto
                    elif key == "GAIN":
                        gain_auto = "AUTO"
                    # white balance auto
                    elif key == "WBAL":
                        wbal_auto =  "AUTO"
                    # led
                    elif key == "LTEN":
                        try:
                            led_en = int(val)
                        except: led_en = 0
                    # ir
                    elif key == "IREN":
                        try:
                            ir_en = int(val)
                        except: ir_en = 0
                    # ir-cut
                    elif key == "IRCT":
                        try:
                            ir_cut = int(val)
                        except: ir_cut = 0
                elif section == "LINK":
                    # baudrate
                    if key == "BAUD":
                        try:
                            b = int(val)
                            if b in ALLOWED_BAUDS:
                                UART_BAUD = b
                        except: UART_BAUD = 115200
        return True
    except OSError:
        save_config();
        return False
    except Exception:
        return False

# ===== Commands: setters & queries =====
# ---- VERSION ----
def get_version():
    send_line("$VERS=%s" % FIRMWARE_VERSION)

# ---- TIME ----
def set_time(val):
    def parse_time_to_tuple(timestr):
        timestr = timestr.strip().replace("/", "-")
        date,clock = timestr.split()
        y,m,d = [int(x) for x in date.split("-")]
        hh,mm,ss = [int(x) for x in clock.split(":")]
        return (y,m,d,0,hh,mm,ss,0)
    try:
        rtc.datetime(parse_time_to_tuple(val))
        y,m,d,wd,hh,mm,ss,sub = rtc.datetime()
        send_line("$TIME=%04d-%02d-%02d %02d:%02d:%02d" % (y,m,d,hh,mm,ss))
    except Exception:
        send_line("$TIME=ERROR")

def get_time():
    y,m,d,wd,hh,mm,ss,sub = rtc.datetime()
    send_line("$TIME=%04d-%02d-%02d %02d:%02d:%02d" % (y,m,d,hh,mm,ss))

# ---- RESO ----
def set_resolution(val):
    global frame_size
    try:
        key = val.strip().upper()
        if key not in RESOLUTION_MAP:  raise ValueError
        frame_size = RESOLUTION_MAP[key]
        sensor.set_framesize(frame_size)
        send_line("$RESO=" + key)
        save_config()
    except Exception:
        send_line("$RESO=ERROR")

def get_resolution():
    name = RESO_NAME.get(frame_size, "UNKNOWN")
    send_line("$RESO=" + name)

# ---- SKIP ----
def set_skip_frames(val):
    global skip_ms
    try:
        ms = int(val)
        if not (100 <= ms <= 5000): raise ValueError
        skip_ms = ms
        sensor.skip_frames(time=skip_ms)
        send_line("$SKIP=%d" % skip_ms)
        save_config()
    except Exception:
        send_line("$SKIP=ERROR")

def get_skip_frames():
    send_line("$SKIP=%d" % skip_ms)

# ---- EXPO ----
def set_expo_us(val):
    global expo_time_us, expo_auto
    v = val.strip().upper()
    try:
        if v == "AUTO":
            expo_auto = True
            expo_time_us = 0
            sensor.set_auto_exposure(True)
            send_line("$EXPO=AUTO")
            save_config()
        else:
            us = int(v)
            if 100 <= us <= 100000:
                expo_auto = False
                expo_time_us = us
                sensor.set_auto_exposure(False, exposure_us=expo_time_us)
                send_line("$EXPO=%d" % expo_time_us)
                save_config()
            else:
                send_line("$EXPO=ERROR")
    except Exception:
        send_line("$EXPO=ERROR")

def get_expo_us():
    send_line("$EXPO=AUTO" if expo_auto else ("$EXPO=%d" % expo_time_us))

# ---- CTST ----
def set_contrast(val):
    global contrast_level
    try:
        v = int(val)
        if v < -3 or v > 3: raise ValueError
        sensor.set_contrast(v)
        send_line("$CTST=%d" % v)
        contrast_level = v
        save_config()
    except Exception:
        send_line("$CTST=ERROR")

def get_contrast():
    send_line("$CTST=%d" % contrast_level)

# ---- BRIT ----
def set_brightness(val):
    global brightness_level
    try:
        v = int(val)
        if v < -3 or v > 3: raise ValueError
        sensor.set_brightness(v)
        send_line("$BRIT=%d" % v)
        brightness_level = v
        save_config()
    except Exception:
        send_line("$BRIT=ERROR")

def get_brightness():
    send_line("$BRIT=%d" % brightness_level)

# ---- SATR ----
def set_saturation(val):
    global saturation_level
    try:
        v = int(val)
        if v < -3 or v > 3: raise ValueError
        #sensor.set_saturation(v)
        send_line("$SATR=%d" % v)
        saturation_level = v
        save_config()
    except Exception:
        send_line("$SATR=ERROR")

def get_saturation():
    send_line("$SATR=%d" % saturation_level)

# ---- GAIN ----
def set_gain_mode(val):
    send_line("$GAIN=AUTO")

def get_gain_mode():
    send_line("$GAIN=AUTO")

# ---- WBAL ----
def set_wbal_mode(val):
    send_line("$WBAL=AUTO")

def get_wbal_mode():
    send_line("$WBAL=AUTO")

# ---- LTEN ----
def set_led(val):
    global led_en
    try:
        v = int(val)
        led_en = v
        send_line("$LTEN=%d" % v)
        save_config()
    except Exception:
        send_line("$LTEN=ERROR")

def get_led():
    send_line("$LTEN=%d" % led_en)

# ---- IREN ----
def set_ir(val):
    global ir_en
    try:
        v = int(val)
        ir_en = v
        send_line("$IREN=%d" % v)
        save_config()
    except Exception:
        send_line("$IREN=ERROR")

def get_ir():
    send_line("$IREN=%d" % ir_en)

# ---- IRCT ----
def set_ircut(val):
    global ir_cut
    try:
        v = int(val)
        ir_cut = v
        send_line("$IRCT=%d" % v)
        save_config()
    except Exception:
        send_line("$IRCT=ERROR")

def get_ircut():
    send_line("$IRCT=%d" % ir_cut)

# ---- BAUD ----
def set_baud(val):
    v = val.strip()
    try:
        b = int(v)
        if b not in ALLOWED_BAUDS:
            send_line("$BAUD=ERROR"); return
        if b == UART_BAUD:
            save_config(); send_line("$BAUD=%d" % b); return
        send_line("$BAUD=SWITCH,%d" % b)
        pyb.delay(50)
        if not uart_init(b):
            send_line("$BAUD=ERROR"); return
        save_config()
        if link_is_usb():
            send_line("$BAUD=%d" % b)
    except Exception:
        send_line("$BAUD=ERROR")

def get_baud():
    send_line("$BAUD=%d" % UART_BAUD)

def ircut_night():
    gpio_cut_p.on(); pyb.delay(20); gpio_cut_p.off()

def ircut_day():
    gpio_cut_p.on(); pyb.delay(20); gpio_cut_p.off()

# ===== Capture JPEG to RAM 0=只拍不存, 1=拍完存到 SD =====
def snapshot_jpeg(val):
    global jpeg, frame_size, skip_ms
    try:
        # 先把 jpeg 清空，避免記憶體不足
        del jpeg
        gc.collect()
        # 初始化
        save_jpg = int(val)
        gpio_led.off(); gpio_ir.off(); gpio_cut_p.off(); gpio_cut_n.off()
        # 暖機：先用 QVGA/200ms，抓一下畫面明暗度
        sensor.set_framesize(sensor.QVGA)
        sensor.skip_frames(time=200)
        #pyb.delay(20)
        img = sensor.snapshot()
        try:
            exp = sensor.get_exposure_us()
            gain = sensor.get_gain_db()
        except:
            exp = 0
            gain = 15
        # 依據亮度決定是否補光
        gain_over = 20
        night_view_en = 0
        if led_en and (gain >= gain_over): gpio_led.on(); night_view_en = 1
        if ir_en and (gain >= gain_over): gpio_ir.on(); night_view_en = 1
        if ir_cut and (gain >= gain_over): ircut_night(); night_view_en = 1
        if night_view_en: pyb.delay(500)
        # 切回目標解析度：用使用者設定 skip_ms
        sensor.set_framesize(frame_size)
        sensor.skip_frames(time=max(200, skip_ms))
        img = sensor.snapshot()
        jpeg = bytes(sensor.get_fb().bytearray())
        del img
        """
        # 取得當前最新幀並導出為不可變 bytes
        try:
            jpeg = bytes(img.compress())
        except Exception:
            jpeg = bytes(img.compress(quality=80))
        """
        # 先存檔，再回覆（依你目前選擇）
        if save_jpg == 1 and jpeg and check_sd_card():
            save_jpeg_to_sd(jpeg)
        send_line("$TAKE=%d" % (len(jpeg) if jpeg else 0))
        #send_line("EXP=%d GAIN=%.1f" % (exp, gain))
    except Exception:
        send_line("$TAKE=ERROR")
    finally:
        gpio_led.off(); gpio_ir.off(); gpio_cut_p.off(); gpio_cut_n.off()

def get_take():
    send_line("$TAKE=%d" % (len(jpeg) if jpeg else 0))

# ===== USB/UART chunked send of JPEG =====
MAGIC = b"#CCH"
CHUNK_USB = 1024
CHUNK_UART = 256

def dump_jpg(start=0, size=None, add_magic=True):
    global jpeg
    if not jpeg: return 0
    n = len(jpeg)
    if start < 0: start = 0
    if start > n: start = n
    end = (start + int(size)) if (size is not None) else n
    if end > n: end = n
    payload_len = end - start
    if payload_len <= 0: return 0
    if add_magic:
        header = MAGIC + struct.pack(">I", payload_len)
        link_send_all(header)
    mv = memoryview(jpeg)
    chunk = CHUNK_USB if link_is_usb() else CHUNK_UART
    i = start
    while i < end:
        j = i + chunk
        if j > end: j = end
        try:
            link_write_once(mv[i:j])
        except TypeError:
            link_write_once(bytes(mv[i:j]))
        i = j
    return payload_len

def get_dump():
    send_line("$DUMP=%d" % (len(jpeg) if jpeg else 0))

# ===== Line I/O （bytes 安全版）=====
def read_line(timeout_ms=1000):
    buf = b""; t0 = pyb.millis()
    while pyb.elapsed_millis(t0) < timeout_ms:
        try:
            if link_is_usb():
                if usb.any():
                    chunk = usb.read()
                    if chunk: buf += chunk
                    if b"\n" in buf: return buf
            if True: #else:
                if uart.any():
                    chunk = uart.read()
                    if chunk: buf += chunk
                    if b"\n" in buf: return buf
        except Exception: pass
        pyb.delay(10)
    return None

def handle_line(buf):
    if not buf: return
    i = buf.find(b"$")
    if i < 0:
        _emit("$EROR=HEADER"); return
    lineb = buf[i:].strip()
    # Query
    if lineb.endswith(b"?"):
        keyb = lineb[:-1].strip().upper()
        if   keyb == b"$VERS": get_version()
        elif keyb == b"$TIME": get_time()
        elif keyb == b"$RESO": get_resolution()
        elif keyb == b"$SKIP": get_skip_frames()
        elif keyb == b"$EXPO": get_expo_us()
        elif keyb == b"$CTST": get_contrast()
        elif keyb == b"$BRIT": get_brightness()
        elif keyb == b"$SATR": get_saturation()
        elif keyb == b"$GAIN": get_gain_mode()
        elif keyb == b"$WBAL": get_wbal_mode()
        elif keyb == b"$LTEN": get_led()
        elif keyb == b"$IREN": get_ir()
        elif keyb == b"$IRCT": get_ircut()
        elif keyb == b"$BAUD": get_baud()
        elif keyb == b"$TAKE": get_take()
        elif keyb == b"$DUMP": get_dump()
        elif keyb == b"$LINK": _emit("$LINK=" + ("USB" if link_is_usb() else "UART"))
        elif keyb == b"$CONF": load_config(); get_resolution(); get_skip_frames(); get_expo_us(); get_contrast(); get_brightness(); get_baud()
        else: _emit("$EROR=CMD")
        return
    # 唯讀 VERS（誤用 '=' 也回覆）
    if lineb.startswith(b"$VERS"):
        get_version; return
    # Setter（需有 '='）
    if b"=" not in lineb:
        _emit("$EROR=SYNTAX"); return
    keyb, valb = lineb.split(b"=", 1)
    keyb = keyb.strip().upper()
    val  = _ascii_clean(valb.strip())
    if   keyb == b"$TIME": set_time(val)
    elif keyb == b"$RESO": set_resolution(val)
    elif keyb == b"$SKIP": set_skip_frames(val)
    elif keyb == b"$EXPO": set_expo_us(val)
    elif keyb == b"$CTST": set_contrast(val)
    elif keyb == b"$BRIT": set_brightness(val)
    elif keyb == b"$SATR": set_saturation(val)
    elif keyb == b"$GAIN": set_gain_mode(val)
    elif keyb == b"$WBAL": set_wbal_mode(val)
    elif keyb == b"$LTEN": set_led(val)
    elif keyb == b"$IREN": set_ir(val)
    elif keyb == b"$IRCT": set_ircut(val)
    elif keyb == b"$BAUD": set_baud(val)
    elif keyb == b"$TAKE": snapshot_jpeg(val)
    elif keyb == b"$DUMP":
        v = val.strip().upper()
        if v == "ALL":
            dump_jpg(0, None, add_magic=True)
        else:
            try:
                parts = [p.strip() for p in val.split(",")]
                if len(parts) == 1:
                    start = int(parts[0]); size = None
                else:
                    start = int(parts[0]); size = int(parts[1])
                dump_jpg(start, size, add_magic=True)
            except Exception:
                _emit("$EROR=DUMP")
    elif keyb == b"$CUTN":
        if val == 0: ircut_day()
        else: ircut_night()
    else: _emit("$EROR=CMD")

# ===== Sensor init =====
def init_sensor():
    # 全域變數
    global frame_size, skip_ms
    global expo_auto, expo_time_us
    global contrast_level, brightness_level, saturation_level
    global gain_auto, gain_db_value
    global wbal_auto, wbal_temp_k, wbal_r_db, wbal_b_db
    # 初始化
    sensor.reset()
    sensor.set_pixformat(sensor.JPEG)
    sensor.set_framesize(sensor.QVGA)
    sensor.skip_frames(time=100)
    pyb.delay(20)
    # JPEG
    try:
        sensor.set_jpeg_quality(80)
    except Exception:
        pass
    # 曝光
    try:
        if expo_auto:
            sensor.set_auto_exposure(True)
        else:
            sensor.set_auto_exposure(False, exposure_us=expo_time_us)
    except Exception:
        pass
    # 增益
    try:
        sensor.set_auto_gain(True)
    except Exception:
        pass
    # 白平衡
    try:
        sensor.set_auto_whitebal(True)
    except Exception:
        pass
    # 對比、亮度、飽和
    try: sensor.set_contrast(contrast_level)
    except Exception: pass
    try: sensor.set_brightness(brightness_level)
    except Exception: pass
    # Stauration function NG
    #try: sensor.set_saturation(saturation_level)
    #except Exception: pass

# ===== Wait flash =====
def wait_flash(timeout_ms=3000):
    t0 = pyb.millis()
    while pyb.elapsed_millis(t0) < timeout_ms:
        try:
            if 'flash' in os.listdir('/'):
                return True
        except:
            pass
        pyb.delay(10)
    return False

# ===== Main =====
def main():

    # Init
    _first = True
    led_white.low()
    INTERVAL_MS = 1000
    next_at = time.ticks_add(time.ticks_ms(), INTERVAL_MS)
    send_line("SYSTEM START")

    # SD hot-plug polling
    SD_POLL_MS = 500
    next_sd_poll = time.ticks_add(time.ticks_ms(), SD_POLL_MS)
    last_sd_present = None

    gpio_cut_p.high()
    gpio_cut_n.high()
    pyb.delay(150)
    gpio_cut_n.low()

    while True:
        # 第一次開機
        if _first:
            # 等 /flash 掛載完成
            # wait_flash()
            # print(os.listdir('/'))
            # 依 config 可能修改 UART 鮑率（在開機階段做，避免連線中途切換問題）
            ok = load_config()
            if UART_BAUD not in ALLOWED_BAUDS or not uart_init(UART_BAUD):
                uart_init(115200)
            send_line("CONFIG " + ("LOADED" if ok else "CREATED"))
            send_line("FIRMWARE VERSION : %s" % FIRMWARE_VERSION)
            # 啟動時若卡已在，嘗試掛上（並以 $CARD= 前綴回報）
            if sd_try_mount():
                send_line("$CARD=START,MOUNTED")
            else:
                send_line("$CARD=START,NOT_PRESENT")
            # 啟動感測器
            send_line("CAMERA START")
            init_sensor()
            send_line("CAMERA DONE")
            # 是否開機抓一張
            # snapshot_jpeg(0)
            _first = False
        # 心跳 LED
        if time.ticks_diff(time.ticks_ms(), next_at) >= 0:
            led_white.value(not led_white.value())
            next_at = time.ticks_add(next_at, INTERVAL_MS)
        # SD 插拔偵測
        if time.ticks_diff(time.ticks_ms(), next_sd_poll) >= 0:
            next_sd_poll = time.ticks_add(next_sd_poll, SD_POLL_MS)
            try:
                present = sd_present()
                if last_sd_present is None:
                    last_sd_present = present  # 初始化
                elif present != last_sd_present:
                    if present:
                        if sd_try_mount():
                            send_line("$CARD=INSERTED,MOUNTED")
                        else:
                            send_line("$CARD=INSERTED,MOUNT_FAIL")
                    else:
                        # 偵測到拔卡：若忙碌則延後卸載
                        if sd_is_busy():
                            send_line("$CARD=REMOVED,BUSY_DEFER")
                        else:
                            if sd_try_unmount(force=True):
                                send_line("$CARD=REMOVED,UMOUNTED")
                            else:
                                send_line("$CARD=REMOVED,UMOUNT_FAIL")
                    last_sd_present = present
                else:
                    # 狀態沒變：如果卡已不在、仍掛載、而且不忙，補做一次卸載
                    if (not present) and sd_is_mounted() and (not sd_is_busy()):
                        if sd_try_unmount(force=True):
                            send_line("$CARD=UMOUNTED_LATE")
            except Exception:
                pass
        # 指令處理
        line = read_line(timeout_ms=200)
        if line is None:
            continue
        handle_line(line)


# ===== 主程式入口 =====
main()
