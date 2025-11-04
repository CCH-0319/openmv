# main.py (OpenMV / MicroPython for OpenMV4 H7 Plus) - v1.2.0
import os, time, pyb, sensor, math
try:
    import ustruct as struct
except ImportError:
    import struct
from pyb import Pin

FIRMWARE_VERSION = "1.2.0"

# ===== GPIO =====
led_red   = pyb.LED(1)
led_green = pyb.LED(2)
led_blue  = pyb.LED(3)
led_white = Pin('PC13', Pin.OUT_PP)

# ===== USB =====
usb = pyb.USB_VCP()

# ===== UART =====
UART_ID = 1
ALLOWED_BAUDS = (9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600)
UART_BAUD = 115200  # 預設值改為 115200
uart = pyb.UART(UART_ID, UART_BAUD, timeout_char=50)  # 8N1

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
skip_ms           = 100            # 0..10000
expo_time_us      = 0              # 0=AUTO; 手動 100..100000 us
expo_auto         = True
contrast_level    = 0              # -3..+3
brightness_level  = 0              # -3..+3
saturation_level  = 0              # -3..+3
gain_auto         = True
gain_db_value     = None           # 浮點 dB（手動時）
wbal_auto         = True
wbal_temp_k       = None           # 手動色溫 K
wbal_r_db         = None
wbal_b_db         = None

# ===== JPEG buffer (bytes) =====
jpeg = None

# ===== Config 檔 =====
CONFIG_PATH  = "/flash/config.ini"
JPEG_FOLDER  = "JPEG"

# ===== SD hot-plug helpers (支持 /sdcard 與 /sd) =====
_sd_dev = None
sd_busy_until = 0  # ticks_ms 截止時間：期間內視為 SD 正忙碌，避免卸載
_SD_MOUNT_CANDIDATES = ('/sdcard', '/sd')
_sd_mount_path = None  # 真正掛載的路徑（/sdcard 或 /sd）

def sd_mark_busy(ms=400):
    """在未來 ms 毫秒內視為 SD 正忙（寫入/flush/建目錄/掛載等）"""
    global sd_busy_until
    sd_busy_until = time.ticks_add(time.ticks_ms(), ms)

def sd_is_busy():
    return time.ticks_diff(sd_busy_until, time.ticks_ms()) > 0

def _root_entries():
    try:
        print(os.listdir('/'))
        return set(os.listdir('/'))
    except Exception:
        return set()

def sd_is_mounted():
    """是否已在 /sdcard 或 /sd 掛載（同時更新 _sd_mount_path）"""
    global _sd_mount_path
    roots = _root_entries()
    for p in _SD_MOUNT_CANDIDATES:
        name = p[1:]  # '/sdcard' -> 'sdcard'
        if name in roots:
            _sd_mount_path = p
            return True
    return False

def sd_mount_path():
    """回傳目前/預計的掛載點：優先 /sdcard，其次 /sd"""
    if sd_is_mounted():
        return _sd_mount_path
    return _SD_MOUNT_CANDIDATES[0]

def sd_present():
    """偵測卡是否存在：有 present() 就用；否則嘗試 init()"""
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
            return False
    except Exception:
        return False

def sd_try_mount():
    """卡在、未掛 → 掛上（優先 /sdcard）；成功回 True"""
    global _sd_dev, _sd_mount_path
    try:
        if sd_is_mounted():
            return True
        if not sd_present():
            return False
        if _sd_dev is None:
            _sd_dev = pyb.SDCard()
        mount_at = sd_mount_path()  # 預設 /sdcard
        os.mount(_sd_dev, mount_at)
        _sd_mount_path = mount_at
        sd_mark_busy(200)
        return True
    except Exception:
        return False

def sd_try_unmount(force=False):
    """卡拔掉或要卸載 → umount；忙碌時若非 force 則延後"""
    global _sd_mount_path
    try:
        if not sd_is_mounted():
            return True
        if (not force) and sd_is_busy():
            return False
        os.umount(_sd_mount_path)
        _sd_mount_path = None
        return True
    except Exception:
        return False

# ===== SD card high level =====
def check_sd_card(folder=JPEG_FOLDER):
    """確保卡已掛載且資料夾存在"""
    try:
        if not sd_is_mounted():
            if not sd_try_mount():
                return False
            sd_mark_busy(200)
        base = sd_mount_path()
        items = set(os.listdir(base))
        if folder not in items:
            os.mkdir('{}/{}'.format(base, folder))
            sd_mark_busy(200)
        return True
    except Exception:
        return False

def save_jpeg_to_sd(jpg_bytes, fname=None, folder=JPEG_FOLDER):
    if not check_sd_card(folder):
        send_line("$CARD=NOT_READY"); return False
    base = sd_mount_path()
    if fname is None:
        y,m,d,wd,hh,mm,ss,sub = rtc.datetime()
        fname = "%04d%02d%02d_%02d%02d%02d.jpg" % (y,m,d,hh,mm,ss)
    fpath = "{}/{}/{}".format(base, folder, fname)
    sd_mark_busy(600)
    try:
        with open(fpath, "wb") as f:
            f.write(jpg_bytes)
            f.flush()
        try:
            os.sync()
        finally:
            sd_mark_busy(600)
        return True
    except Exception:
        send_line("$CARD=WRITE_FAILED")
        return False

# ===== 通訊抽象層：USB 優先，否則 UART =====
def link_is_usb():
    try:
        return bool(usb and usb.isconnected())
    except Exception:
        return False

def link_any():
    if link_is_usb():
        try: return usb.any()
        except Exception: return 0
    else:
        try: return uart.any()
        except Exception: return 0

def link_read(n=None):
    if link_is_usb(): return usb.read(n) if n else usb.read()
    else: return uart.read(n) if n else uart.read()

def link_write_once(buf, timeout=5000):
    # 允許 bytes / bytearray / memoryview
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
def _emit(msg): send_line(msg)

def _ascii_clean(b):
    out = bytearray()
    for x in b:
        if 32 <= x <= 126 or x in (9, 10, 13): out.append(x)
    return out.decode()

# ===== Kelvin → RGB gains (approx) =====
def _clip(v, lo, hi): return lo if v < lo else (hi if v > hi else v)

def kelvin_to_rgb(temp_k):
    t = _clip(int(temp_k), 1000, 40000) / 100.0
    if t <= 66: r = 255.0
    else: r = _clip(329.698727446 * ((t - 60.0) ** -0.1332047592), 0.0, 255.0)
    if t <= 66: g = _clip(99.4708025861 * math.log(t) - 161.1195681661, 0.0, 255.0)
    else: g = _clip(288.1221695283 * ((t - 60.0) ** -0.0755148492), 0.0, 255.0)
    if t >= 66: b = 255.0
    elif t <= 19: b = 0.0
    else: b = _clip(138.5177312231 * math.log(t - 10.0) - 305.0447927307, 0.0, 255.0)
    return (r, g, b)

def rgb_to_db(r, g, b):
    eps = 1e-6
    rg = max(r, eps) / max(g, eps)
    bg = max(b, eps) / max(g, eps)
    r_db = 20.0 * math.log(rg) / math.log(10.0)
    b_db = 20.0 * math.log(bg) / math.log(10.0)
    r_db = _clip(r_db, -24.0, 24.0)
    b_db = _clip(b_db, -24.0, 24.0)
    return (r_db, 0.0, b_db)

# ===== Config 存取 =====
def _now_str():
    y,m,d,wd,hh,mm,ss,sub = rtc.datetime()
    return "%04d-%02d-%02d %02d:%02d:%02d" % (y,m,d,hh,mm,ss)

def save_config():
    try:
        reso_name = RESO_NAME.get(frame_size, "VGA")
        expo_str  = "AUTO" if expo_auto else str(int(expo_time_us))
        cfg = []
        cfg.append("# OpenMV Camera Config")
        cfg.append("# Saved: " + _now_str())
        cfg.append("[CAMERA]")
        cfg.append("RESO=" + reso_name)
        cfg.append("SKIP=" + str(int(skip_ms)))
        cfg.append("EXPO=" + expo_str)
        cfg.append("CTST=" + str(int(contrast_level)))
        cfg.append("BRIT=" + str(int(brightness_level)))
        cfg.append("SATR=" + str(int(saturation_level)))
        cfg.append("GAIN=" + ("AUTO" if gain_auto else "OFF"))
        if (not gain_auto) and (gain_db_value is not None):
            cfg.append("GAIN_DB=" + ("%.2f" % gain_db_value))
        cfg.append("WBAL=" + ("AUTO" if wbal_auto else "OFF"))
        if (not wbal_auto):
            if wbal_temp_k is not None:
                cfg.append("WBAL_TEMP=" + str(int(wbal_temp_k)))
            elif (wbal_r_db is not None) and (wbal_b_db is not None):
                cfg.append("WB_R_DB=" + ("%.2f" % wbal_r_db))
                cfg.append("WB_B_DB=" + ("%.2f" % wbal_b_db))
        cfg.append("")
        cfg.append("[LINK]")
        cfg.append("BAUD=" + str(int(UART_BAUD)))
        cfg.append("")
        with open(CONFIG_PATH, "w") as f:
            f.write("\n".join(cfg)); f.flush()
        try: os.sync()
        except Exception: pass
        return True
    except Exception:
        return False

def load_config():
    global frame_size, skip_ms, expo_time_us, expo_auto
    global contrast_level, brightness_level, saturation_level
    global gain_auto, gain_db_value
    global wbal_auto, wbal_temp_k, wbal_r_db, wbal_b_db
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
                    if key == "RESO":
                        k = val.upper()
                        if k in RESOLUTION_MAP: frame_size = RESOLUTION_MAP[k]
                    elif key == "SKIP":
                        try:
                            ms = int(val)
                            if 0 <= ms <= 10000: skip_ms = ms
                        except: pass
                    elif key == "EXPO":
                        v = val.upper()
                        if v == "AUTO":
                            expo_auto = True; expo_time_us = 0
                        else:
                            try:
                                us = int(v)
                                if 100 <= us <= 100000:
                                    expo_auto = False; expo_time_us = us
                            except: pass
                    elif key == "CTST":
                        try:
                            c = int(val)
                            if -3 <= c <= 3: contrast_level = c
                        except: pass
                    elif key == "BRIT":
                        try:
                            b = int(val)
                            if -3 <= b <= 3: brightness_level = b
                        except: pass
                    elif key == "SATR":
                        try:
                            t = int(val)
                            if -3 <= t <= 3: saturation_level = t
                        except: pass
                    elif key == "GAIN":
                        gain_auto = (val.upper() == "AUTO")
                    elif key == "GAIN_DB":
                        try:
                            gain_db_value = float(val)
                        except: pass
                    elif key == "WBAL":
                        wbal_auto = (val.upper() == "AUTO")
                    elif key == "WBAL_TEMP":
                        try:
                            wbal_temp_k = int(val)
                        except: pass
                    elif key == "WB_R_DB":
                        try:
                            wbal_r_db = float(val)
                        except: pass
                    elif key == "WB_B_DB":
                        try:
                            wbal_b_db = float(val)
                        except: pass
                elif section == "LINK":
                    if key == "BAUD":
                        try:
                            b = int(val)
                            if b in ALLOWED_BAUDS:
                                UART_BAUD = b
                        except: pass
        return True
    except OSError:
        save_config(); return False
    except Exception:
        return False

# ===== Commands: setters & queries =====
# ---- TIME ----
def set_time(val):
    def parse_time_to_tuple(timestr):
        timestr = timestr.strip().replace("/", "-")
        date, clock = timestr.split()
        y, m, d = [int(x) for x in date.split("-")]
        hh, mm, ss = [int(x) for x in clock.split(":")]
        return (y, m, d, 0, hh, mm, ss, 0)
    try:
        rtc.datetime(parse_time_to_tuple(val))
        y,m,d,wd,hh,mm,ss,sub = rtc.datetime()
        send_line("$TIME=%04d-%02d-%02d %02d:%02d:%02d" % (y,m,d,hh,mm,ss))
    except Exception:
        send_line("$TIME=ERROR")

def q_time():
    y,m,d,wd,hh,mm,ss,sub = rtc.datetime()
    send_line("$TIME=%04d-%02d-%02d %02d:%02d:%02d" % (y,m,d,hh,mm,ss))

# ---- RESO ----
def set_resolution(val):
    global frame_size
    key = val.strip().upper()
    if key not in RESOLUTION_MAP:
        send_line("$RESO=ERROR"); return
    try:
        frame_size = RESOLUTION_MAP[key]
        sensor.set_framesize(frame_size)
        save_config(); send_line("$RESO=" + key)
    except Exception:
        send_line("$RESO=ERROR")

def q_reso():
    name = RESO_NAME.get(frame_size, "UNKNOWN")
    send_line("$RESO=" + name)

# ---- SKIP ----
def set_skip_frames(val):
    global skip_ms
    try:
        ms = int(val)
        if not (0 <= ms <= 10000): raise ValueError
        skip_ms = ms
        sensor.skip_frames(time=skip_ms)
        save_config(); send_line("$SKIP=%d" % skip_ms)
    except Exception:
        send_line("$SKIP=ERROR")

def q_skip():
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
            save_config()
            send_line("$EXPO=AUTO")
        else:
            us = int(v)
            if 100 <= us <= 100000:
                expo_auto = False
                expo_time_us = us
                sensor.set_auto_exposure(False, exposure_us=expo_time_us)
                save_config()
                send_line("$EXPO=%d" % expo_time_us)
            else:
                send_line("$EXPO=ERROR")
    except Exception:
        send_line("$EXPO=ERROR")

def q_expo():
    send_line("$EXPO=AUTO" if expo_auto else "$EXPO=%d" % expo_time_us)

# ---- CTST ----
def set_contrast(val):
    global contrast_level
    try:
        v = int(val)
        if v < -3 or v > 3: raise ValueError
        sensor.set_contrast(v); contrast_level = v
        save_config(); send_line("$CTST=%d" % v)
    except Exception:
        send_line("$CTST=ERROR")

def q_contrast(): send_line("$CTST=%d" % contrast_level)

# ---- BRIT ----
def set_brightness(val):
    global brightness_level
    try:
        v = int(val)
        if v < -3 or v > 3: raise ValueError
        sensor.set_brightness(v); brightness_level = v
        save_config(); send_line("$BRIT=%d" % v)
    except Exception:
        send_line("$BRIT=ERROR")

def q_brightness(): send_line("$BRIT=%d" % brightness_level)

# ---- SATR ----
def set_saturation(val):
    """$SATR = -3..+3"""
    global saturation_level
    try:
        v = int(val)
        if v < -3 or v > 3: raise ValueError
        #sensor.set_saturation(v)
        saturation_level = v
        save_config(); send_line("$SATR=%d" % v)
    except Exception:
        send_line("$SATR=ERROR")

def q_saturation(): send_line("$SATR=%d" % saturation_level)

# ---- GAIN ----
def set_gain_mode(val):
    """$GAIN=AUTO | OFF[,<gain_db>]"""
    global gain_auto, gain_db_value
    v = val.strip().upper()
    try:
        if v == "AUTO":
            sensor.set_auto_gain(True)
            gain_auto = True; gain_db_value = None
            save_config(); send_line("$GAIN=AUTO")
        else:
            parts = [p.strip() for p in v.split(",")]
            if parts[0] != "OFF": raise ValueError
            gain_db_value = None
            if len(parts) > 1 and parts[1] != "":
                db = float(parts[1])
                if db < 0.0: db = 0.0
                if db > 30.0: db = 30.0
                sensor.set_auto_gain(False, gain_db=db)
                gain_db_value = db
            else:
                sensor.set_auto_gain(False)
            gain_auto = False
            save_config()
            send_line("$GAIN=" + ("OFF,%.2f" % gain_db_value if gain_db_value is not None else "OFF"))
    except Exception:
        send_line("$GAIN=ERROR")

def q_gain():
    if gain_auto: send_line("$GAIN=AUTO")
    else:
        if gain_db_value is None: send_line("$GAIN=OFF")
        else: send_line("$GAIN=OFF,%.2f" % gain_db_value)

# ---- WBAL ----
def set_wbal_mode(val):
    """$WBAL=AUTO | OFF[,<tempK>]"""
    global wbal_auto, wbal_temp_k, wbal_r_db, wbal_b_db
    v = val.strip().upper()
    try:
        if v == "AUTO":
            sensor.set_auto_whitebal(True)
            wbal_auto = True
            wbal_temp_k = None; wbal_r_db = None; wbal_b_db = None
            save_config(); send_line("$WBAL=AUTO")
        else:
            parts = [p.strip() for p in v.split(",")]
            if parts[0] != "OFF": raise ValueError
            wbal_auto = False
            wbal_r_db = wbal_b_db = None
            if len(parts) > 1 and parts[1] != "":
                tempK = int(parts[1])
                tempK = _clip(tempK, 2000, 12000)
                r,g,b = kelvin_to_rgb(tempK)
                r_db, g_db, b_db = rgb_to_db(r,g,b)
                sensor.set_auto_whitebal(False, rgb_gain_db=(r_db, g_db, b_db))
                wbal_temp_k = tempK
                wbal_r_db, wbal_b_db = r_db, b_db
                save_config(); send_line("$WBAL=OFF,%d" % tempK)
            else:
                sensor.set_auto_whitebal(False)
                wbal_temp_k = None
                save_config(); send_line("$WBAL=OFF")
    except Exception:
        send_line("$WBAL=ERROR")

def q_wbal():
    if wbal_auto: send_line("$WBAL=AUTO")
    else:
        if wbal_temp_k is not None: send_line("$WBAL=OFF,%d" % wbal_temp_k)
        elif (wbal_r_db is not None) and (wbal_b_db is not None):
            send_line("$WBAL=OFF,R=%.2fdB,B=%.2fdB" % (wbal_r_db, wbal_b_db))
        else:
            send_line("$WBAL=OFF")

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

def q_baud():
    send_line("$BAUD=%d" % UART_BAUD)

# ---- VERS ----
def q_version(): send_line("$VERS=%s" % FIRMWARE_VERSION)

# ===== Capture JPEG to RAM =====
def snapshot_jpeg(val):
    """val: 0=只拍不存, 1=拍完存到 SD"""
    global jpeg, frame_size, skip_ms
    try:
        save_jpg = int(val)
        led_red.off(); led_green.off(); led_blue.off()
        # 暖機：先用 QVGA，至少 200ms（或使用者設定的更長值）
        sensor.set_framesize(sensor.QVGA)
        sensor.skip_frames(time=min(200, skip_ms))
        sensor.snapshot()
        # 切回目標解析度：用使用者設定 skip_ms
        sensor.set_framesize(frame_size)
        sensor.skip_frames(time=skip_ms)
        # 取得當前最新幀並導出為不可變 bytes
        img = sensor.snapshot()
        try:
            jpeg = bytes(img.compress())
        except Exception:
            jpeg = bytes(img.compress(quality=80))
        led_red.off(); led_green.off(); led_blue.off()
        # 先存檔，再回覆（依你目前選擇）
        if save_jpg == 1 and jpeg and check_sd_card():
            save_jpeg_to_sd(jpeg)
        send_line("$TAKE=%d" % (len(jpeg) if jpeg else 0))
    except Exception:
        send_line("$TAKE=ERROR")
    finally:
        led_red.off(); led_green.on(); led_blue.off()

def q_take():
    send_line("$TAKE=%d" % (len(jpeg) if jpeg else 0))

# ===== USB/UART chunked send of JPEG =====
MAGIC = b"#CCH"; CHUNK_USB = 1024; CHUNK_UART = 256

def dump_jpg(start=0, size=None, add_magic=True):
    global jpeg
    if not jpeg:
        return 0
    n = len(jpeg)
    if start < 0: start = 0
    if start > n: start = n
    end = (start + int(size)) if (size is not None) else n
    if end > n: end = n
    payload_len = end - start
    if payload_len <= 0:
        return 0
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

def q_dump():
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
            else:
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
        if   keyb == b"$TIME": q_time()
        elif keyb == b"$RESO": q_reso()
        elif keyb == b"$SKIP": q_skip()
        elif keyb == b"$EXPO": q_expo()
        elif keyb == b"$TAKE": q_take()
        elif keyb == b"$DUMP": q_dump()
        elif keyb == b"$LINK": _emit("$LINK=" + ("USB" if link_is_usb() else "UART"))
        elif keyb == b"$CTST": q_contrast()
        elif keyb == b"$BRIT": q_brightness()
        elif keyb == b"$SATR": q_saturation()
        elif keyb == b"$GAIN": q_gain()
        elif keyb == b"$WBAL": q_wbal()
        elif keyb == b"$BAUD": q_baud()
        elif keyb == b"$VERS": q_version()
        else: _emit("$EROR=CMD")
        return
    # 唯讀 VERS（誤用 '=' 也回覆）
    if lineb.startswith(b"$VERS"):
        _emit("$VERS=%s" % FIRMWARE_VERSION); return
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
    elif keyb == b"$CTST": set_contrast(val)
    elif keyb == b"$BRIT": set_brightness(val)
    elif keyb == b"$SATR": set_saturation(val)
    elif keyb == b"$GAIN": set_gain_mode(val)
    elif keyb == b"$WBAL": set_wbal_mode(val)
    elif keyb == b"$BAUD": set_baud(val)
    else:
        _emit("$EROR=CMD")

# ===== Sensor init =====
def init_sensor():
    global frame_size, skip_ms, expo_auto, expo_time_us
    global contrast_level, brightness_level, saturation_level
    global gain_auto, gain_db_value
    global wbal_auto, wbal_temp_k, wbal_r_db, wbal_b_db

    sensor.reset()
    sensor.set_pixformat(sensor.JPEG)
    sensor.set_framesize(frame_size)
    try:
        sensor.set_jpeg_quality(80)   # 預設品質；可改由 config 控制
    except Exception:
        pass
    #sensor.skip_frames(time=skip_ms)

    # 曝光
    if expo_auto:
        sensor.set_auto_exposure(True)
    else:
        sensor.set_auto_exposure(False, exposure_us=expo_time_us)

    # 增益
    try:
        if gain_auto:
            sensor.set_auto_gain(True)
        else:
            if gain_db_value is not None:
                sensor.set_auto_gain(False, gain_db=gain_db_value)
            else:
                sensor.set_auto_gain(False)
    except Exception:
        pass

    # 白平衡
    try:
        if wbal_auto:
            sensor.set_auto_whitebal(True)
        else:
            if wbal_temp_k is not None:
                r,g,b = kelvin_to_rgb(wbal_temp_k)
                r_db, g_db, b_db = rgb_to_db(r,g,b)
                sensor.set_auto_whitebal(False, rgb_gain_db=(r_db, g_db, b_db))
                wbal_r_db, wbal_b_db = r_db, b_db
            elif (wbal_r_db is not None) and (wbal_b_db is not None):
                sensor.set_auto_whitebal(False, rgb_gain_db=(wbal_r_db, 0.0, wbal_b_db))
            else:
                sensor.set_auto_whitebal(False)
    except Exception:
        pass

    # 對比、亮度、飽和
    try: sensor.set_contrast(contrast_level)
    except Exception: pass
    try: sensor.set_brightness(brightness_level)
    except Exception: pass
    #try: sensor.set_saturation(saturation_level)
    #except Exception: pass

# ===== Main =====
def main():
    send_line('System Power ON')
    _first = True
    led_white.low()
    INTERVAL_MS = 1000
    next_at = time.ticks_add(time.ticks_ms(), INTERVAL_MS)

    # === SD hot-plug polling ===
    SD_POLL_MS = 500
    next_sd_poll = time.ticks_add(time.ticks_ms(), SD_POLL_MS)
    last_sd_present = None

    while True:
        if _first:
            ok = load_config()
            # 依 config 可能修改 UART 鮑率（在開機階段做，避免連線中途切換問題）
            if UART_BAUD not in ALLOWED_BAUDS or not uart_init(UART_BAUD):
                uart_init(115200)
            send_line("Config " + ("Loaded" if ok else "Created"))
            led_red.off(); led_green.on(); led_blue.off()
            send_line("Firmware Version : %s" % FIRMWARE_VERSION)
            # 啟動時若卡已在，嘗試掛上（並以 $CARD= 前綴回報）
            if sd_try_mount():
                send_line("$CARD=START,MOUNTED")
            else:
                send_line("$CARD=START,NOT_PRESENT")
            send_line('Camera Start'); init_sensor(); send_line('Camera Done')
            _first = False

        # 心跳 LED
        if time.ticks_diff(time.ticks_ms(), next_at) >= 0:
            led_white.value(not led_white.value())
            next_at = time.ticks_add(next_at, INTERVAL_MS)

        # === SD 插拔偵測 ===
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


main()
