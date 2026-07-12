import sys
import time
import datetime
import threading
import json
import requests
from pathlib import Path
import ctypes
import os
import tempfile
from cryptography.fernet import Fernet
import base64
import websocket
import ssl  # 新增：引入 ssl 模組以處理憑證問題

import cv2
import numpy as np
import psutil
import win32gui
import win32ui
import win32con
import win32process
import win32api  
import torch
from ultralytics import YOLO

import mss  # 引入極速截圖套件

# 引入介面與模擬操作庫
import tkinter as tk
from tkinter import messagebox
import customtkinter as ctk
import keyboard

# -------------------------- 設定與全域變數 --------------------------
MODEL_PATH = "best.encrypted"         # 你的加密 YOLO 模型檔名
DECRYPT_KEY = b'jizoqQB-4kZtFGkPSVSukevD4b8vwJGVTI4cNFtnSLw='

TARGET_PROCESS = "PlayTogether.exe"   # 目標進程名稱
CONFIDENCE = 0.4                      # 全域置信度閾值 (適用於驚嘆號、大魚等)
CONFIDENCE_MAN = 0.75                 # 專屬「背包滿了」的高置信度閾值 (避免誤判)
CONFIDENCE_PAOGAN = 0.82              # 專屬「拋竿」的高置信度閾值
CONFIDENCE_AO = 0.3                  # 凹凸置信度
CONFIDENCE_BIG_FISH = 0.75            # 專屬「暈眩」的高置信度閾值
CONFIDENCE_CHECK = 0.45               # 判斷大魚血條的高置信度閾值
DETECT_INTERVAL = 0.05                # 檢測間隔（秒）
CONFIG_FILE = "settings.json"         # 儲存設定的檔案名稱

# 船錨模板設定
ANCHOR_TEMPLATE_PATH = "船錨.png"    # 船錨圖片檔名
ANCHOR_THRESHOLD = 0.6                # 船錨匹配相似度閾值 (0.0 ~ 1.0)

# 類別對應
CLASS_AO = 0         # 凹凸
CLASS_KEEP = 1       # 保管
CLASS_STUN = 2       # 大鱼
CLASS_FISH = 3       # 惊叹号
CLASS_BIG_FISH = 4   # 晕眩
CLASS_MAN = 5        # 背包满了
CLASS_WEIXIU = 6     # 维修
CLASS_XIAGAN = 7     # 下杆了 (舊的備用 / 維修判定用)
CLASS_PAOGAN = 8     # 拋竿 (主要使用的下竿按鈕)

# 狀態機定義
STATE_WAIT_FISH = 0  # 狀態 1：等待框選區內的驚嘆號
STATE_CHECK_MODE = 1 # 狀態 2：按下左鍵後，全螢幕檢測是大魚還是保管
STATE_BIG_FISH = 2   # 狀態 3：大魚模式，全螢幕檢測驚嘆號與暈眩
STATE_RECAST = 3     # 狀態 4：保管後，檢查防掛並重新拋竿

# 全域設定參數
config = {
    "roi": None,          
    "click_x": 0,         
    "click_y": 0,         
    "fish_roi": None,     # 魚類背景框選範圍
    "mutant_roi": None,   # 變異檢測框選範圍
    "hotkey": "f10",      
    "drag_hotkey": "f8",  
    "webhook_url": "",    # DC Webhook 網址
    "is_running": False,
    "current_state": STATE_WAIT_FISH,
    "has_clicked_paogan": False, 
    "need_roi_recal": False,    
    "enable_roi_recal": True,   
    "count_fish": 0,
    "count_big": 0,
    "count_keep": 0,
    "count_weixiu": 0,   
    "count_mutant": 0,    # 變異計數
    "count_man_fail": 0,
    "count_paogan_fail": 0, 
    "quick_sell": False,
    "license_key": "",
    "overlay_x": 150,  # 新增：懸浮窗預設 X 座標
    "overlay_y": 150   # 新增：懸浮窗預設 Y 座標
}

# ==================== 防迷路與解密防護機制 ====================
def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

def load_encrypted_model(encrypted_model_path):
    print(f"🔍 系統正在嘗試讀取的模型路徑為: {encrypted_model_path}")
    if not os.path.exists(encrypted_model_path):
        print("❌ 慘了，系統確認這個路徑下找不到檔案！請檢查路徑或檔名。")
        sys.exit(1)
        
    cipher_suite = Fernet(DECRYPT_KEY)
    
    with open(encrypted_model_path, 'rb') as file:
        encrypted_data = file.read()
        
    decrypted_data = cipher_suite.decrypt(encrypted_data)
    
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.pt')
    temp_path = temp_file.name
    temp_file.write(decrypted_data)
    temp_file.close()
    
    try:
        print("🛡️ [系統] 正在從安全通道載入模型...")
        model = YOLO(temp_path)
        return model
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
            print("🔧 [系統] 實體暫存模型已物理銷毀！")
# ====================================================================

# 載入船錨模板圖片
anchor_template = None
anchor_path = resource_path(ANCHOR_TEMPLATE_PATH)
if os.path.exists(anchor_path):
    anchor_template = cv2.imread(anchor_path)
    print(f"⚓ [系統] 成功載入船錨比對圖片。")
else:
    print(f"⚠️ [警告] 找不到 {ANCHOR_TEMPLATE_PATH}，船錨自動放下功能將無法運作！")

# 檢查收竿位置周圍 240x240 是否有船錨圖標
def check_anchor_pulled(frame, click_x, click_y):
    global anchor_template
    if anchor_template is None or click_x == 0 or click_y == 0:
        return False
        
    h, w, _ = frame.shape
    x1 = max(0, click_x - 120)
    y1 = max(0, click_y - 120)
    x2 = min(w, click_x + 120)
    y2 = min(h, click_y + 120)
    
    roi = frame[y1:y2, x1:x2]
    if roi.size == 0:
        return False
        
    res = cv2.matchTemplate(roi, anchor_template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, _ = cv2.minMaxLoc(res)
    
    if max_val >= ANCHOR_THRESHOLD:
        return True
    return False

# -------------------------- 設定檔儲存與讀取機制 --------------------------
def load_config():
    try:
        if Path(CONFIG_FILE).exists():
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                saved_data = json.load(f)
                if "hotkey" in saved_data: config["hotkey"] = saved_data["hotkey"]
                if "drag_hotkey" in saved_data: config["drag_hotkey"] = saved_data["drag_hotkey"]
                if "webhook_url" in saved_data: config["webhook_url"] = saved_data["webhook_url"]
                if "quick_sell" in saved_data: config["quick_sell"] = saved_data["quick_sell"]
                if "enable_roi_recal" in saved_data: config["enable_roi_recal"] = saved_data["enable_roi_recal"]
                if "roi" in saved_data and saved_data["roi"]: config["roi"] = tuple(saved_data["roi"])
                if "click_x" in saved_data: config["click_x"] = saved_data["click_x"]
                if "click_y" in saved_data: config["click_y"] = saved_data["click_y"]
                if "fish_roi" in saved_data and saved_data["fish_roi"]: config["fish_roi"] = tuple(saved_data["fish_roi"])
                if "mutant_roi" in saved_data and saved_data["mutant_roi"]: config["mutant_roi"] = tuple(saved_data["mutant_roi"])
                if "license_key" in saved_data: config["license_key"] = saved_data["license_key"]
                if "overlay_x" in saved_data: config["overlay_x"] = saved_data["overlay_x"]
                if "overlay_y" in saved_data: config["overlay_y"] = saved_data["overlay_y"]
    except Exception as e:
        print(f"讀取設定檔失敗: {e}")

def save_config():
    try:
        data_to_save = {
            "hotkey": config["hotkey"],
            "drag_hotkey": config["drag_hotkey"],
            "webhook_url": config["webhook_url"],
            "quick_sell": config.get("quick_sell", False),
            "enable_roi_recal": config.get("enable_roi_recal", True),
            "roi": config.get("roi"),
            "click_x": config.get("click_x"),
            "click_y": config.get("click_y"),
            "fish_roi": config.get("fish_roi"),
            "mutant_roi": config.get("mutant_roi"),
            "license_key": config.get("license_key", ""),
            "overlay_x": config.get("overlay_x", 150),
            "overlay_y": config.get("overlay_y", 150)
        }
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(data_to_save, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"儲存設定檔失敗: {e}")

load_config()

# -------------------------- Discord Webhook 發送工具 --------------------------
def send_discord_webhook(url, msg, img=None):
    if not url or url.strip() == "":
        return
        
    def task():
        try:
            if img is not None:
                success, buffer = cv2.imencode('.jpg', img)
                if success:
                    files = {'file': ('screenshot.jpg', buffer.tobytes(), 'image/jpeg')}
                    data = {'content': msg}
                    requests.post(url, data=data, files=files, timeout=10)
            else:
                data = {'content': msg}
                requests.post(url, json=data, timeout=10)
        except Exception as e:
            print(f"Discord Webhook 發送失敗: {e}")
            
    threading.Thread(target=task, daemon=True).start()

# -------------------------- 視窗截圖工具 (MSS 極速版) --------------------------
sct = mss.MSS()

def get_window_by_process(process_name):
    for proc in psutil.process_iter(['pid', 'name']):
        if proc.info['name'] == process_name:
            pid = proc.info['pid']
            def enum_callback(hwnd, hwnd_list):
                if win32gui.IsWindowVisible(hwnd):
                    _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
                    if found_pid == pid:
                        hwnd_list.append(hwnd)
            hwnd_list = []
            win32gui.EnumWindows(enum_callback, hwnd_list)
            if hwnd_list:
                return hwnd_list[0]
    return None

def capture_window(hwnd):
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width = right - left
        height = bottom - top

        if width <= 0 or height <= 0:
            return None

        monitor = {"top": top, "left": left, "width": width, "height": height}
        sct_img = sct.grab(monitor)
        img = np.array(sct_img)

        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    except Exception as e:
        return None

# -------------------------- 模擬操作工具 --------------------------
def win_click(hwnd, x, y):
    try:
        pure_x = int(round(float(x)))
        pure_y = int(round(float(y)))
        
        win_l, win_t, win_r, win_b = win32gui.GetWindowRect(hwnd)
        
        screen_x = win_l + pure_x
        screen_y = win_t + pure_y
        
        win32api.SetCursorPos((screen_x, screen_y))
        time.sleep(0.01)  
        
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.02)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        
    except Exception as e:
        pass

def win_fast_click(hwnd, x, y, times=20):
    try:
        pure_x = int(round(float(x)))
        pure_y = int(round(float(y)))
        win_l, win_t, win_r, win_b = win32gui.GetWindowRect(hwnd)
        win32api.SetCursorPos((win_l + pure_x, win_t + pure_y))
        
        for _ in range(times):
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 2, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 4, 0, 0, 0)

    except Exception as e:
        pass

# -------------------------- 框選與坐標獲取遮罩 --------------------------
class SelectionOverlay:
    def __init__(self, hwnd, callback, mode="roi_fixed"):
        self.hwnd = hwnd
        self.callback = callback
        self.mode = mode 
        
        self.root = tk.Toplevel()
        self.root.attributes("-alpha", 0.3)
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-topmost", True)
        self.root.config(cursor="cross")
        
        self.canvas = tk.Canvas(self.root, cursor="cross", bg="grey")
        self.canvas.pack(fill="both", expand=True)
        
        self.start_x = None
        self.start_y = None
        self.rect = None

        if self.mode == "roi_fixed":
            self.fixed_w = 65
            self.fixed_h = 85
            self.rect = self.canvas.create_rectangle(-100, -100, -100, -100, outline="red", width=2)
            self.canvas.bind("<Motion>", self.on_motion_fixed)
            self.canvas.bind("<ButtonRelease-1>", self.on_release_fixed)
            
        elif self.mode == "roi_free":
            self.canvas.bind("<ButtonPress-1>", self.on_press)
            self.canvas.bind("<B1-Motion>", self.on_drag)
            self.canvas.bind("<ButtonRelease-1>", self.on_release)
            
        elif self.mode == "point":
            self.canvas.bind("<ButtonPress-1>", self.on_press)
            self.canvas.bind("<ButtonRelease-1>", self.on_release)

    def on_motion_fixed(self, event):
        x1 = event.x - self.fixed_w // 2
        y1 = event.y - self.fixed_h // 2
        x2 = event.x + self.fixed_w // 2
        y2 = event.y + self.fixed_h // 2
        self.canvas.coords(self.rect, x1, y1, x2, y2)

    def on_release_fixed(self, event):
        x1 = event.x - self.fixed_w // 2
        y1 = event.y - self.fixed_h // 2
        x2 = event.x + self.fixed_w // 2
        y2 = event.y + self.fixed_h // 2
        self.root.destroy()
        
        win_l, win_t, _, _ = win32gui.GetWindowRect(self.hwnd)
        self.callback((x1 - win_l, y1 - win_t, x2 - win_l, y2 - win_t))

    def on_press(self, event):
        self.start_x, self.start_y = event.x, event.y
        if self.mode == "roi_free":
            self.rect = self.canvas.create_rectangle(self.start_x, self.start_y, event.x, event.y, outline="red", width=2)

    def on_drag(self, event):
        if self.mode == "roi_free" and self.rect:
            self.canvas.coords(self.rect, self.start_x, self.start_y, event.x, event.y)

    def on_release(self, event):
        end_x, end_y = event.x, event.y
        self.root.destroy()
        
        win_l, win_t, _, _ = win32gui.GetWindowRect(self.hwnd)
        
        if self.mode == "roi_free":
            x1, x2 = sorted([self.start_x - win_l, end_x - win_l])
            y1, y2 = sorted([self.start_y - win_t, end_y - win_t])
            self.callback((x1, y1, x2, y2))
        elif self.mode == "point":
            cx = end_x - win_l
            cy = end_y - win_t
            self.callback((cx, cy))

# -------------------------- 魚種稀有度判斷函數 --------------------------
def detect_fish_rarity(frame, roi_coords, mutant_roi_coords=None):
    if not roi_coords:
        return "🐟 判定結果：未知 (未設定背景範圍)", False
        
    x1, y1, x2, y2 = roi_coords
    y1, y2 = max(0, y1), min(frame.shape[0], y2)
    x1, x2 = max(0, x1), min(frame.shape[1], x2)
    roi_img = frame[y1:y2, x1:x2]
    
    if roi_img.size == 0:
        return "🐟 判定結果：未知 (範圍錯誤)", False

    bg_type = "普通底"
    hsv_img = cv2.cvtColor(roi_img, cv2.COLOR_BGR2HSV)
    lower_purple = np.array([108, 60, 140])   
    upper_purple = np.array([165, 255, 255]) 
    mask_purple = cv2.inRange(hsv_img, lower_purple, upper_purple)
    matched_pixels_purple = cv2.countNonZero(mask_purple)
    total_pixels = roi_img.shape[0] * roi_img.shape[1]
    
    if (matched_pixels_purple / total_pixels) > 0.15:
        bg_type = "紫/彩底" 

    mutant_type = None
    is_mutant = False
    
    if mutant_roi_coords:
        mx1, my1, mx2, my2 = mutant_roi_coords
        my1, my2 = max(0, my1), min(frame.shape[0], my2)
        mx1, mx2 = max(0, mx1), min(frame.shape[1], mx2)
        mutant_img = frame[my1:my2, mx1:mx2]
        
        if mutant_img.size > 0:
            hsv_mutant = cv2.cvtColor(mutant_img, cv2.COLOR_BGR2HSV)
            mutant_colors = [
                ("潮濕", np.array([90,  80, 150]), np.array([115, 255, 255])),
                ("電擊", np.array([25,  80, 150]), np.array([45,  255, 255])),
                ("乾燥", np.array([10,  80, 150]), np.array([25,  255, 255])),
                ("月光", np.array([125, 80, 150]), np.array([140, 255, 255])),
                ("極光", np.array([145, 80, 150]), np.array([170, 255, 255])),
                ("寒氣", np.array([110, 80, 150]), np.array([125, 255, 255]))
            ]

            for name, lower, upper in mutant_colors:
                mask = cv2.inRange(hsv_mutant, lower, upper)
                contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                
                for cnt in contours:
                    if cv2.contourArea(cnt) > 80: 
                        mutant_type = name
                        is_mutant = True
                        break
                if mutant_type:
                    break

    if is_mutant:
        return f"🐟 判定結果：變異魚類 (✨{mutant_type}變異✨ + {bg_type})", True
    else:
        return f"🐟 判定結果：普通魚類 ({bg_type})", False

# -------------------------- 主核心邏輯 (全智能循環自動釣魚) --------------------------
def main_automation_loop(model, hwnd, gui):
    global config
    gui.log("[系統] 自動化核心執行緒已啟動 (極速截圖模式)。")
    
    last_big_fish_click_time = 0
    BIG_FISH_CLICK_COOLDOWN = 1.0  

    last_keep_time = 0
    KEEP_EFFECT_DELAY = 3.5
    was_paused = False

    while True:
        if not config["is_running"]:
            time.sleep(0.5)
            continue

        # ==================== 定時暫停與復原機制 ====================
        now = datetime.datetime.now().time()
        # 設定暫停的起訖時間 (時, 分, 秒)
        pause_start = datetime.time(7, 59, 59)
        pause_end = datetime.time(8, 0, 59)
        
        # 1. 如果時間在暫停區間內
        if pause_start <= now <= pause_end:
            if not was_paused:
                gui.log("⏰ [系統] 進入等待八點更新 (07:59:50 ~ 08:00:15)。")
            
            gui.update_status("狀態：等待八點更新", "yellow")
            was_paused = True  # 標記目前正在暫停中
            time.sleep(1.0)
            continue
            
        # 2. 如果時間離開暫停區間，且剛剛有暫停過 (代表暫停剛結束)
        if was_paused:
            gui.log("⏰ [系統] 等待八點更新！自動按下一次空白鍵。")
            # 模擬按下空白鍵
            win32api.keybd_event(0x20, 0x39, 0, 0)
            time.sleep(0.05)
            win32api.keybd_event(0x20, 0x39, win32con.KEYEVENTF_KEYUP, 0)
            
            was_paused = False  # 重置標記，避免重複按空白鍵
            time.sleep(1.0)     # 按完後稍微等 1 秒，讓遊戲畫面或每日獎勵結算反應一下
        # ================================================================
            
        try:
            full_frame = capture_window(hwnd)
            if full_frame is None: 
                time.sleep(0.05)
                continue
            
            results = model(full_frame, conf=CONFIDENCE, verbose=False)
            
            if not config["is_running"]:
                continue

            detected_classes = []
            has_fish_in_roi = False      
            fish_conf = 0.0              
            trigger_conf = 0.0           
            keep_button_pos = None       
            paogan_pos = None
            weixiu_pos = None            
            
            for r in results:
                for box in r.boxes:
                    cls_id = int(box.cls[0])
                    conf = float(box.conf[0])
                    
                    if cls_id in [CLASS_STUN, CLASS_KEEP] and conf < CONFIDENCE_CHECK:
                            continue
                    if cls_id == CLASS_MAN and conf < CONFIDENCE_MAN:
                        continue
                    if cls_id == CLASS_PAOGAN and conf < CONFIDENCE_PAOGAN:
                        continue
                    if cls_id == CLASS_AO and conf < CONFIDENCE_AO:
                        continue
                    if cls_id == CLASS_BIG_FISH and conf < CONFIDENCE_BIG_FISH:
                        continue

                    detected_classes.append(cls_id)
                    
                    xyxy = box.xyxy[0].cpu().numpy()
                    cx = int((xyxy[0] + xyxy[2]) / 2)
                    cy = int((xyxy[1] + xyxy[3]) / 2)
                    
                    if cls_id == CLASS_FISH:
                        if config["roi"]:
                            rx1, ry1, rx2, ry2 = config["roi"]
                            if rx1 <= cx <= rx2 and ry1 <= cy <= ry2:
                                has_fish_in_roi = True
                                fish_conf = conf  
                            elif config.get("need_roi_recal") and config.get("enable_roi_recal"):
                                old_cx = (rx1 + rx2) / 2
                                old_cy = (ry1 + ry2) / 2
                                dist = ((cx - old_cx) ** 2 + (cy - old_cy) ** 2) ** 0.5
                                TOLERANCE_RADIUS = 110 
                                
                                if dist <= TOLERANCE_RADIUS:
                                    has_fish_in_roi = True
                                    fish_conf = conf
                                    w = rx2 - rx1
                                    h = ry2 - ry1
                                    config["roi"] = (int(cx - w/2), int(cy - h/2), int(cx + w/2), int(cy + h/2))
                                    gui.log(f"🔄 [尋標校正] 抓到新位置！自動平移追蹤框 (位移 {int(dist)} 像素)。")
                                    config["need_roi_recal"] = False
                        else:
                            has_fish_in_roi = True
                            fish_conf = conf
                            
                        trigger_conf = conf       
                        
                    elif cls_id == CLASS_KEEP:
                        keep_button_pos = (cx, cy)
                    elif cls_id == CLASS_PAOGAN:
                        paogan_pos = (cx, cy)
                    elif cls_id == CLASS_WEIXIU:
                        weixiu_pos = (cx, cy)
                    elif cls_id == CLASS_BIG_FISH: 
                        trigger_conf = conf       

            if config["current_state"] == STATE_WAIT_FISH and config.get("roi"):
                win_l, win_t, _, _ = win32gui.GetWindowRect(hwnd)
                rx1, ry1, rx2, ry2 = config["roi"]
                screen_x1, screen_y1 = win_l + rx1, win_t + ry1
                screen_x2, screen_y2 = win_l + rx2, win_t + ry2
                gui.show_roi_box(screen_x1, screen_y1, screen_x2, screen_y2)
            else:
                gui.hide_roi_box()

            has_man = CLASS_MAN in detected_classes
            has_ao = CLASS_AO in detected_classes
            has_paogan = CLASS_PAOGAN in detected_classes
            has_weixiu = CLASS_WEIXIU in detected_classes
            has_xiagan = CLASS_XIAGAN in detected_classes

            # ==============================================================
            # 【專屬獨立：等待模式】
            # ==============================================================
            if config["current_state"] == STATE_WAIT_FISH:
                config["count_man_fail"] = 0  
                gui.update_status("狀態：等待驚嘆號", "cyan")

                # 🚀 [船錨機制] 等待模式檢測船錨 🚀
                if not has_paogan and check_anchor_pulled(full_frame, config["click_x"], config["click_y"]):
                    gui.log("⚓ [船錨防呆] 等待模式中偵測到船錨被拉起！自動點擊放下船錨並準備重新拋竿...")
                    # 💡 發送通知至 Discord，並附上當下截圖
                    send_discord_webhook(config["webhook_url"], "⚓ **[自動防護] 偵測到船錨被拉起！** 已嘗試重新放下船錨。", full_frame)
                    win_click(hwnd, config["click_x"], config["click_y"])
                    time.sleep(2.0)
                    config["has_clicked_paogan"] = False
                    config["current_state"] = STATE_RECAST
                    continue
                
                if has_ao:
                    gui.log("⚠️ [等待模式] 偵測到防掛(凹凸)！自動解題...")
                    win32api.keybd_event(0x20, 0x39, 0, 0)
                    time.sleep(0.05)
                    win32api.keybd_event(0x20, 0x39, win32con.KEYEVENTF_KEYUP, 0)
                    time.sleep(1.0)
                    continue

                if has_paogan and paogan_pos:
                    config["count_paogan_fail"] += 1
                    gui.log(f"🎣 [等待模式] 尚未到達保管階段即需重新拋竿... (循環失敗: {config['count_paogan_fail']}/3)")
                    
                    if config["count_paogan_fail"] >= 3:
                        gui.log("❌ [全局防呆] 連續 3 次未完成完整釣魚循環，腳本自動停止。")
                        send_discord_webhook(config["webhook_url"], "🚨 **腳本停止通知** 🚨\n連續 3 次未成功到達「保管」階段，可能卡住了，自動保護機制已將腳本停止！")
                        gui.stop_script()
                        config["count_paogan_fail"] = 0
                        continue
                        
                    win_click(hwnd, paogan_pos[0], paogan_pos[1])
                    time.sleep(1.5) 
                    continue
                
                if has_fish_in_roi:
                    gui.log(f"🎣 [智慧觸發] 發現驚嘆號！點擊自訂收竿位置")
                    win_click(hwnd, config["click_x"], config["click_y"])
                    
                    config["count_fish"] += 1
                    gui.update_stats()
                    config["current_state"] = STATE_CHECK_MODE
                    time.sleep(0.5) 
                    continue
                
                time.sleep(DETECT_INTERVAL)
                continue

            # ==============================================================
            # 【全局最高優先級防呆機制】
            # ==============================================================
            if has_man and config["current_state"] == STATE_RECAST:
                gui.log(f"⚠️ [防呆] 拋竿時偵測到背包滿了！嘗試關閉提示... (連續失敗: {config['count_man_fail']}/3)")
                if paogan_pos:
                    win_click(hwnd, paogan_pos[0], paogan_pos[1])
                time.sleep(2.0)
                
                check_frame = capture_window(hwnd)
                if check_frame is not None:
                    check_results = model(check_frame, conf=CONFIDENCE, verbose=False)
                    check_classes = [int(box.cls[0]) for r in check_results for box in r.boxes]
                    is_waiting = (CLASS_PAOGAN not in check_classes) and (CLASS_MAN not in check_classes)
                    
                    if not is_waiting:
                        config["count_man_fail"] += 1
                        if config["count_man_fail"] >= 3:
                            gui.log("❌ [全局防呆] 連續 3 次無法解除滿包提示，腳本自動停止。")
                            send_discord_webhook(config["webhook_url"], "🚨 **腳本停止通知** 🚨\n連續 3 次拋竿被背包滿了阻擋，自動保護機制已將腳本停止！")
                            gui.stop_script()
                            config["count_man_fail"] = 0
                    else:
                        gui.log("✅ 成功關閉滿包提示，重置拋竿狀態。")
                        config["count_man_fail"] = 0
                        config["current_state"] = STATE_RECAST
                        config["has_clicked_paogan"] = False
                continue

            if has_ao:
                gui.log("⚠️ [全局防呆] 偵測到防掛(凹凸)！前台按下空白鍵解題")
                win32api.keybd_event(0x20, 0x39, 0, 0)
                time.sleep(0.05)
                win32api.keybd_event(0x20, 0x39, win32con.KEYEVENTF_KEYUP, 0)
                config["has_clicked_paogan"] = False 
                config["current_state"] = STATE_RECAST 
                time.sleep(1.0)
                continue

            if has_weixiu and has_xiagan and config["current_state"] == STATE_RECAST:
                gui.log("🔧 [全局防呆] 拋竿時偵測到釣竿損毀！前台按下空白鍵進行維修")
                win32api.keybd_event(0x20, 0x39, 0, 0)
                time.sleep(0.05)
                win32api.keybd_event(0x20, 0x39, win32con.KEYEVENTF_KEYUP, 0)
                time.sleep(1.0)
                gui.log("🎣 [全局防呆] 維修完畢，再次按下空白鍵準備下竿")
                win32api.keybd_event(0x20, 0x39, 0, 0)
                time.sleep(0.05)
                win32api.keybd_event(0x20, 0x39, win32con.KEYEVENTF_KEYUP, 0)
                
                config["count_weixiu"] += 1   
                gui.update_stats()            
                config["has_clicked_paogan"] = False 
                config["current_state"] = STATE_RECAST 
                time.sleep(0.8)
                continue

            if config["current_state"] not in [STATE_RECAST, STATE_WAIT_FISH]:
                if has_paogan and paogan_pos:
                    gui.log(f"🎣 [全局補救] 異常中斷！發現尚未拋竿，進入拋竿檢視流程")
                    config["has_clicked_paogan"] = False
                    config["current_state"] = STATE_RECAST
                    continue

            # ----------------- 狀態 2：全螢幕判定大魚或保管 -----------------
            if config["current_state"] == STATE_CHECK_MODE:
                gui.update_status("狀態：判斷大魚/保管...", "yellow")
                
                if CLASS_STUN in detected_classes:
                    gui.log("💥 [狀態切換] 偵測到大魚，進入大魚模式！")
                    config["count_big"] += 1
                    gui.update_stats()
                    config["current_state"] = STATE_BIG_FISH
                    last_big_fish_click_time = 0 
                    
                elif CLASS_KEEP in detected_classes:
                    config["count_paogan_fail"] = 0 
                    is_rare = False
                    
                    if config.get("fish_roi"):
                        time.sleep(0.1) 
                        fresh_frame = capture_window(hwnd)
                        if fresh_frame is not None:
                            rarity_result, is_mutant = detect_fish_rarity(fresh_frame, config["fish_roi"], config.get("mutant_roi"))
                            gui.log(rarity_result)
                            
                            if is_mutant:
                                config["count_mutant"] += 1
                                is_rare = True
                            
                            if "紫/彩底" in rarity_result:
                                is_rare = True
                                msg = f"✨ **驚喜！釣到稀有魚啦！** {rarity_result}，請查看截圖："
                                send_discord_webhook(config["webhook_url"], msg, fresh_frame)
                    else:
                        gui.log("⏩ [略過檢測] 未設定魚類判斷範圍，預設為普通處理。")

                    should_sell = config["quick_sell"] and not is_rare

                    if keep_button_pos:
                        time.sleep(0.5) 
                        if should_sell:
                            gui.log("💰 [賣出] 執行快速賣魚 。")
                            win_click(hwnd, keep_button_pos[0] - 150, keep_button_pos[1])
                            time.sleep(0.5) 
                            win32api.keybd_event(0x20, 0x39, 0, 0)
                            time.sleep(0.05)
                            win32api.keybd_event(0x20, 0x39, win32con.KEYEVENTF_KEYUP, 0)
                            time.sleep(0.5)
                        else:
                            gui.log("🎒 [保管] 點擊保管按鈕。")
                            win_click(hwnd, keep_button_pos[0], keep_button_pos[1])
                    else:
                        time.sleep(0.5)
                        if should_sell:
                            gui.log("💰 [賣出] 執行快速賣魚 。")
                            win_click(hwnd, config["click_x"] - 150, config["click_y"])
                            time.sleep(0.5) 
                            win32api.keybd_event(0x20, 0x39, 0, 0)
                            time.sleep(0.05)
                            win32api.keybd_event(0x20, 0x39, win32con.KEYEVENTF_KEYUP, 0)
                            time.sleep(0.5)
                        else:
                            gui.log("🎒 [保管] 盲點保管位置。")
                            win_click(hwnd, config["click_x"], config["click_y"])
                        
                    config["count_keep"] += 1
                    gui.update_stats()
                    time.sleep(0.5)  
                    config["has_clicked_paogan"] = False  
                    config["current_state"] = STATE_RECAST
                    last_keep_time = time.time()
                    continue
                    
                
            # ----------------- 狀態 3：大魚模式 -----------------
            elif config["current_state"] == STATE_BIG_FISH:
                gui.update_status("狀態：大魚模式中...", "red")
                
                if CLASS_KEEP in detected_classes:
                    config["count_paogan_fail"] = 0 
                    gui.log("📸 [大魚模式] 等待 2 秒確保大魚畫面清晰...")
                    time.sleep(2.0) 
                    
                    fresh_frame = capture_window(hwnd)
                    if fresh_frame is None: fresh_frame = full_frame  
                    send_discord_webhook(config["webhook_url"], "🎣 **釣到大魚啦！** 準備保管，請查看截圖：", fresh_frame)
                    
                    if keep_button_pos:
                        time.sleep(0.5)
                        win_click(hwnd, keep_button_pos[0], keep_button_pos[1])
                    else:
                        win_click(hwnd, config["click_x"], config["click_y"])
                        
                    config["count_keep"] += 1
                    gui.update_stats()
                    time.sleep(0.5) 
                    gui.log("⚠️ [系統] 大魚結束，準備重新拋竿...")
                    
                    config["has_clicked_paogan"] = False  
                    config["current_state"] = STATE_RECAST
                    continue
                
                elif CLASS_BIG_FISH in detected_classes:
                    win_fast_click(hwnd, config["click_x"], config["click_y"], times=5)
                    last_big_fish_click_time = time.time() 
                    
                elif CLASS_FISH in detected_classes:
                    current_time = time.time()
                    if current_time - last_big_fish_click_time > BIG_FISH_CLICK_COOLDOWN:
                        win_click(hwnd, config["click_x"], config["click_y"])
                        last_big_fish_click_time = current_time 

            # ----------------- 狀態 4：保管/維修後拋竿與安全檢測 -----------------
            elif config["current_state"] == STATE_RECAST:
                status_text = f"狀態：檢視拋竿/防掛[{'V' if has_paogan else 'X'}]"
                gui.update_status(status_text, "cyan")
                
                has_keep = CLASS_KEEP in detected_classes
                
                if has_ao:
                    gui.log("⚠️ [狀態4 防呆] 拋竿前偵測到防掛(凹凸)！暫停拋竿，優先解題...")
                    win32api.keybd_event(0x20, 0x39, 0, 0)
                    time.sleep(0.05)
                    win32api.keybd_event(0x20, 0x39, win32con.KEYEVENTF_KEYUP, 0)
                    time.sleep(1.0) 
                    continue 
                    
                if has_keep:
                    gui.log("⚠️ [狀態4 防呆] 拋竿前發現殘留「保管」！嘗試進行補點...")
                    if keep_button_pos:
                        win_click(hwnd, keep_button_pos[0], keep_button_pos[1])
                    else:
                        win_click(hwnd, config["click_x"], config["click_y"])
                    time.sleep(1.0) 
                    continue 

                # 🚀 [船錨機制] 拋竿前檢測船錨 🚀
                is_effect_cooling_down = (time.time() - last_keep_time) < KEEP_EFFECT_DELAY
                
                if not has_paogan and not is_effect_cooling_down and check_anchor_pulled(full_frame, config["click_x"], config["click_y"]):
                    gui.update_status("狀態：無拋竿鈕，檢測船錨中...", "yellow")
                    gui.log("⚓ [船錨防呆] 拋竿前偵測到船錨被拉起！自動點擊收竿位置重新放下船錨...")
                    # 💡 發送通知至 Discord，並附上當下截圖
                    send_discord_webhook(config["webhook_url"], "⚓ **[自動防護] 偵測到船錨被拉起！** 已嘗試重新放下船錨。", full_frame)
                    win_click(hwnd, config["click_x"], config["click_y"])
                    time.sleep(2.0)
                    continue

                if has_paogan:
                    if paogan_pos:
                        gui.log("🎣 [檢視拋竿] 點擊拋竿按鈕...")
                        win_click(hwnd, paogan_pos[0], paogan_pos[1])
                        config["has_clicked_paogan"] = True 
                        time.sleep(1.5) 
                    continue
                else:
                    if config["has_clicked_paogan"]:
                        time.sleep(0.5)
                        gui.log("✅ 確認成功拋竿下水，開啟尋標校正...")
                        config["has_clicked_paogan"] = False 
                        config["current_state"] = STATE_WAIT_FISH
                        config["need_roi_recal"] = True

            time.sleep(DETECT_INTERVAL)
            
        except Exception as e:
            gui.log(f"[迴圈錯誤] {e}")
            time.sleep(1)

# -------------------------- 自訂範圍追蹤框 (Roi Overlay) --------------------------
class RoiOverlay:
    def __init__(self, parent_root):
        self.root = tk.Toplevel(parent_root)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", "black") 
        self.root.config(bg="black")
        self._set_click_through(True)
        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.rect = self.canvas.create_rectangle(0, 0, 0, 0, outline="#00FFFF", width=2, dash=(4, 4)) 
        self.root.withdraw() 

    def update_box(self, x1, y1, x2, y2):
        w = max(1, x2 - x1)
        h = max(1, y2 - y1)
        self.root.geometry(f"{w}x{h}+{x1}+{y1}")
        self.canvas.coords(self.rect, 0, 0, w, h)
        self.root.deiconify()

    def hide(self):
        self.root.withdraw()

    def _set_click_through(self, enable):
        try:
            self.root.update_idletasks()
            hwnd = win32gui.GetParent(self.root.winfo_id())
            if not hwnd: hwnd = self.root.winfo_id()
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            if enable:
                win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, style | win32con.WS_EX_TRANSPARENT | win32con.WS_EX_LAYERED)
            else:
                win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, style & ~win32con.WS_EX_TRANSPARENT)
        except Exception as e:
            pass

# -------------------------- 多行狀態懸浮視窗 (Status Overlay) --------------------------
class StatusOverlay:
    def __init__(self, parent_root):
        self.root = tk.Toplevel(parent_root)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        
        self.width = 650
        self.height = 120 
        pos_x = config.get("overlay_x", 150)
        pos_y = config.get("overlay_y", 150)
        self.root.geometry(f"{self.width}x{self.height}+{pos_x}+{pos_y}") 
        
        self.bg_color = "#000002"
        self.root.config(bg=self.bg_color)
        
        self.canvas = tk.Canvas(self.root, bg=self.bg_color, highlightthickness=0)
        self.canvas.pack(expand=True, fill="both")
        
        self.is_drag_mode = False
        self.status_text = "狀態：已停止"
        self.status_color = "gray"
        
        self.fish_count = 0
        self.big_count = 0
        self.keep_count = 0
        self.weixiu_count = 0
        self.mutant_count = 0
        self.run_time_str = "00:00:00"
        
        self.canvas.bind("<ButtonPress-1>", self.start_move)
        self.canvas.bind("<ButtonRelease-1>", self.stop_move)
        self.canvas.bind("<B1-Motion>", self.do_move)
        
        keyboard.add_hotkey(config["drag_hotkey"], self.toggle_mode_safe)
        self.root.after(200, self.set_passthrough)

    def start_move(self, event):
        self.x = event.x
        self.y = event.y

    def stop_move(self, event):
        self.x = None
        self.y = None
        
        # 當滑鼠放開停止拖曳時，儲存當下視窗座標
        config["overlay_x"] = self.root.winfo_x()
        config["overlay_y"] = self.root.winfo_y()
        save_config()

    def do_move(self, event):
        if self.is_drag_mode and self.x is not None and self.y is not None:
            deltax = event.x - self.x
            deltay = event.y - self.y
            x = self.root.winfo_x() + deltax
            y = self.root.winfo_y() + deltay
            self.root.geometry(f"+{x}+{y}")

    def toggle_mode_safe(self):
        self.root.after(0, self._toggle_mode)

    def _toggle_mode(self):
        self.is_drag_mode = not self.is_drag_mode
        if self.is_drag_mode:
            self.set_drag_mode()
        else:
            self.set_passthrough()

    def set_drag_mode(self):
        self.root.deiconify()  
        self.root.attributes("-transparentcolor", "")
        self._set_click_through(False)
        self.canvas.delete("all")
        self.canvas.config(bg="yellow")
        font_setting = ("Microsoft JhengHei", 14, "bold") 
        text = f"🟨 拖曳模式 (用滑鼠拖動，完成後再按 {config['drag_hotkey'].upper()} 鎖定)"
        self.canvas.create_text(self.width // 2, self.height // 2, text=text, fill="black", font=font_setting, justify="center")

    def set_passthrough(self):
        self.root.attributes("-transparentcolor", self.bg_color)
        self._set_click_through(True)
        self.refresh_display()
        if not config["is_running"]:
            self.root.withdraw()

    def update_status_data(self, text, color="white"):
        self.status_text = text
        self.status_color = color
        self.refresh_display()

    def update_stats_data(self, fish, big, keep, weixiu, mutant):
        self.fish_count = fish
        self.big_count = big
        self.keep_count = keep
        self.weixiu_count = weixiu
        self.mutant_count = mutant
        self.refresh_display()

    def update_timer_data(self, time_str):
        self.run_time_str = time_str
        self.refresh_display()

    def refresh_display(self):
        if not self.is_drag_mode:
            display_color = self.status_color
            if display_color == "cyan": display_color = "#00FFFF"
            elif display_color == "green": display_color = "#00FF00"
            elif display_color == "red": display_color = "#FF3333"
            elif display_color == "yellow": display_color = "#FFFF00"
            elif display_color == "gray": display_color = "#CCCCCC"
            
            header_text = f"{self.status_text}"
            stats_text = f"驚嘆號:{self.fish_count} 大魚:{self.big_count} 保管:{self.keep_count} 維修:{self.weixiu_count} 變異:{self.mutant_count}"
            timer_text = f"⏱ 運行：{self.run_time_str}"
            
            self.root.after(0, lambda: self.render_normal_mode(header_text, stats_text, timer_text, display_color))

    def render_normal_mode(self, header_text, stats_text, timer_text, header_color):
        self.canvas.delete("all")
        self.canvas.config(bg=self.bg_color)
        font_main = ("Microsoft JhengHei", 14, "bold")
        font_stats = ("Microsoft JhengHei", 13, "bold")
        
        center_x = self.width // 2
        
        self._draw_outlined_text(center_x, 25, header_text, header_color, font_main)
        self._draw_outlined_text(center_x, 60, stats_text, "white", font_stats)
        self._draw_outlined_text(center_x, 95, timer_text, "#00FF66", font_main)

    def _draw_outlined_text(self, x, y, text, color, font):
        outline_color = "black"
        for dx, dy in [(-1,-1), (-1,1), (1,-1), (1,1), (-2,0), (2,0), (0,-2), (0,2)]:
            self.canvas.create_text(x+dx, y+dy, text=text, fill=outline_color, font=font, justify="center")
        self.canvas.create_text(x, y, text=text, fill=color, font=font, justify="center")

    def _set_click_through(self, enable):
        try:
            self.root.update_idletasks()
            hwnd = win32gui.GetParent(self.root.winfo_id())
            if not hwnd: hwnd = self.root.winfo_id()
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            if enable:
                win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, style | win32con.WS_EX_TRANSPARENT | win32con.WS_EX_LAYERED)
            else:
                win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, style & ~win32con.WS_EX_TRANSPARENT)
        except Exception as e:
            pass

def start_websocket_client(gui_instance):
    user_key = config.get("license_key", "").strip()
    if not user_key:
        gui_instance.log("⚠️ 警告：未偵測到儲存的卡密，連線已取消。請確保登入時有輸入卡密。")
        return
        
    SERVER_URL = "wss://ooze.zeabur.app/ws"

    def on_message(ws, message):
        try:
            data = json.loads(message)
            action = data.get("action")
            
            if action == "start":
                gui_instance.root.after(0, gui_instance.start_script)
                
            elif action == "stop":
                gui_instance.root.after(0, gui_instance.stop_script)
                
            elif action == "broadcast": 
                msg = data.get("msg", "")
                gui_instance.root.after(0, lambda: messagebox.showinfo("官方廣播", msg))
                
            elif action == "killswitch": 
                msg = data.get("msg", "您已被管理員從伺服器強制踢除。")
                ws.close()
                gui_instance.root.after(0, lambda: messagebox.showerror("授權終止", msg))
                os._exit(0) 
            elif action in ["restart", "update"]:
                gui_instance.log("🔄 收到重啟/更新指令！準備執行免驗證重啟...")
                
                import subprocess
                import sys
                
                current_exe = sys.executable 
                
                # 判斷是否為 Python 環境開發測試，或是 Nuitka 打包出來的 exe
                if current_exe.endswith("python.exe") or current_exe.endswith("pythonw.exe"):
                    subprocess.Popen([current_exe, sys.argv[0], f"--auto-login={user_key}"])
                else:
                    subprocess.Popen([current_exe, f"--auto-login={user_key}"])
                
                # 關閉目前的程序
                os._exit(0)

            elif action == "screenshot":
                frame = capture_window(gui_instance.hwnd)
                if frame is not None:
                    success, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
                    if success:
                        b64_str = base64.b64encode(buffer).decode('utf-8')
                        ws.send(json.dumps({
                            "action": "screenshot_return",
                            "image": b64_str
                        }))
                else:
                    pass    
        except Exception as e:
            pass

    def on_open(ws):
        ws.send(json.dumps({"user_id": user_key}))
        
        def heartbeat():
            while ws.keep_running:
                try:
                    ws.send(json.dumps({"action": "ping"}))
                    time.sleep(20)
                except:
                    break
        threading.Thread(target=heartbeat, daemon=True).start()

    # ======== 👇 關鍵修改：連線錯誤時彈出警告視窗 (2秒自動關閉) 👇 ========
    def on_error(ws, error):
        err_msg = str(error)
        print(f" 錯誤: {err_msg}") 
        
        def show_error():
            err_win = ctk.CTkToplevel(gui_instance.root)
            err_win.title("連線異常")
            err_win.geometry("400x220")
            err_win.attributes("-topmost", True)
            err_win.resizable(False, False)
            
            msg = f"無法連線至伺服器！\n您的連線可能遭到防毒軟體、防火牆或電信商攔截。\n\n請嘗試關閉防毒軟體或使用手機熱點連線。\n\n詳細錯誤：\n{err_msg}"
            lbl = ctk.CTkLabel(err_win, text=msg, text_color="red", justify="left", wraplength=360)
            lbl.pack(expand=True, padx=20, pady=20)
            
            # 設定 2 秒後自動銷毀錯誤提示窗
            err_win.after(2000, err_win.destroy)
            
        gui_instance.root.after(0, show_error)
    # ========================================================

    def run():
        while True:
            ws = websocket.WebSocketApp(
                SERVER_URL, 
                on_open=on_open, 
                on_message=on_message,
                on_error=on_error
            )
            # ▼▼▼ 修改這裡：加上 sslopt 參數略過憑證檢查 ▼▼▼
            ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
            time.sleep(5) 

    threading.Thread(target=run, daemon=True).start()

# -------------------------- GUI 介面設計 (極簡版) --------------------------
class FishingGUI:
    def __init__(self, model, hwnd):
        self.model = model
        self.hwnd = hwnd
        self.run_time_seconds = 0  
        
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        
        self.root = ctk.CTk()
        self.root.title("🎣 承承 自動釣魚輔助")
        self.root.geometry("400x700") 
        self.root.attributes("-topmost", False) 
        
        try:
            icon_path = resource_path("001.ico")
            self.root.iconbitmap(icon_path)
        except Exception as e:
            pass
        
        self.overlay = StatusOverlay(self.root)
        self.overlay.root.withdraw()
        
        title = ctk.CTkLabel(self.root, text="🎣 承承 自動釣魚輔助", font=("Microsoft JhengHei", 24, "bold"))
        title.pack(pady=10)
        
        controls = ctk.CTkFrame(self.root)
        controls.pack(fill="x", padx=15, pady=5)
        
        self.btn_roi = ctk.CTkButton(controls, text="設定驚嘆號範圍", command=self.start_roi_selection)
        self.btn_roi.pack(fill="x", padx=10, pady=2)
        self.lbl_roi = ctk.CTkLabel(controls, text="未設定範圍", text_color="gray")
        if config.get("roi"): self.lbl_roi.configure(text=f"已載入範圍: {config['roi']}", text_color="green")
        self.lbl_roi.pack()
        
        self.btn_pos = ctk.CTkButton(controls, text="定收竿點擊位置 (驚嘆號/暈眩用)", command=self.start_pos_selection)
        self.btn_pos.pack(fill="x", padx=10, pady=2)
        self.lbl_pos = ctk.CTkLabel(controls, text="未設定位置", text_color="gray")
        if config.get("click_x") and config.get("click_y"): self.lbl_pos.configure(text=f"已載入位置: (X={config['click_x']}, Y={config['click_y']})", text_color="green")
        self.lbl_pos.pack()

        self.btn_fish_roi = ctk.CTkButton(controls, text="設定魚類背景範圍 (判斷稀有度)", command=self.start_fish_roi_selection)
        self.btn_fish_roi.pack(fill="x", padx=10, pady=2)
        self.lbl_fish_roi = ctk.CTkLabel(controls, text="未設定魚類範圍", text_color="gray")
        if config.get("fish_roi"): self.lbl_fish_roi.configure(text=f"已載入魚類範圍", text_color="green")
        self.lbl_fish_roi.pack()

        self.btn_mutant_roi = ctk.CTkButton(controls, text="設定變異判斷範圍", command=self.start_mutant_roi_selection)
        self.btn_mutant_roi.pack(fill="x", padx=10, pady=2)
        self.lbl_mutant_roi = ctk.CTkLabel(controls, text="未設定變異範圍", text_color="gray")
        if config.get("mutant_roi"): self.lbl_mutant_roi.configure(text=f"已載入變異範圍", text_color="green")
        self.lbl_mutant_roi.pack()
        
        self.chk_quick_sell_var = ctk.BooleanVar(value=config.get("quick_sell", False))
        self.chk_quick_sell = ctk.CTkCheckBox(controls, text="快速賣魚 (不保留紫/彩底以下的魚)", variable=self.chk_quick_sell_var, command=self.toggle_quick_sell)
        self.chk_quick_sell.pack(fill="x", padx=10, pady=5)

        self.chk_roi_recal_var = ctk.BooleanVar(value=config.get("enable_roi_recal", True))
        self.chk_roi_recal = ctk.CTkCheckBox(controls, text="尋找驚嘆號 (拋竿後自動修正位置)", variable=self.chk_roi_recal_var, command=self.toggle_roi_recal)
        self.chk_roi_recal.pack(fill="x", padx=10, pady=5)
        
        hotkey_frame = ctk.CTkFrame(controls, fg_color="transparent")
        hotkey_frame.pack(pady=2)
        ctk.CTkLabel(hotkey_frame, text="啟動/停止:").pack(side="left", padx=2)
        self.entry_hotkey = ctk.CTkEntry(hotkey_frame, width=50)
        self.entry_hotkey.insert(0, config["hotkey"])
        self.entry_hotkey.pack(side="left", padx=2)
        ctk.CTkLabel(hotkey_frame, text="拖曳模式:").pack(side="left", padx=2)
        self.entry_drag_hotkey = ctk.CTkEntry(hotkey_frame, width=50)
        self.entry_drag_hotkey.insert(0, config["drag_hotkey"])
        self.entry_drag_hotkey.pack(side="left", padx=2)
        
        webhook_frame = ctk.CTkFrame(controls, fg_color="transparent")
        webhook_frame.pack(pady=2, fill="x", padx=10)
        ctk.CTkLabel(webhook_frame, text="DC Webhook:").pack(side="left", padx=2)
        self.entry_webhook = ctk.CTkEntry(webhook_frame)
        self.entry_webhook.insert(0, config.get("webhook_url", ""))
        self.entry_webhook.pack(side="left", fill="x", expand=True, padx=2)
        
        ctk.CTkButton(controls, text="儲存設定", width=100, command=self.bind_new_hotkey).pack(pady=5)

        btns = ctk.CTkFrame(self.root)
        btns.pack(fill="x", padx=15, pady=5)
        self.btn_start = ctk.CTkButton(btns, text="▶ 啟動", fg_color="green", hover_color="darkgreen", command=self.start_script)
        self.btn_start.pack(side="left", expand=True, padx=5)
        self.btn_stop = ctk.CTkButton(btns, text="⏸ 暫停", fg_color="red", hover_color="darkred", command=self.stop_script)
        self.btn_stop.pack(side="left", expand=True, padx=5)

        self.log_box = ctk.CTkTextbox(self.root, height=130)
        self.log_box.pack(fill="both", expand=True, padx=15, pady=5)
        
        keyboard.add_hotkey(config["hotkey"], self.toggle_script)
        
        self.timer_loop()
        self.thread = threading.Thread(target=main_automation_loop, args=(self.model, self.hwnd, self), daemon=True)
        self.thread.start()
        
        self.log("系統初始化完成，已自動載入先前設定。")
        
        # ▼▼▼ 啟動 GUI 介面時自動觸發連線 ▼▼▼
        start_websocket_client(self)
        
        try:
            if torch.cuda.is_available():
                gpu_name = torch.cuda.get_device_name(0)
                self.log(f"🚀 [推理引擎] 啟用 GPU 加速：{gpu_name}")
            else:
                self.log("🐌 [推理引擎] 目前使用 CPU 推理 (未偵測到 CUDA)")
        except Exception as e:
            self.log(f"🔍 [推理引擎] 硬體狀態檢測失敗: {e}")
        
        self.log(f"💡 提示：按 {config['drag_hotkey'].upper()} 可以移動螢幕懸浮視窗。")
        
        self.roi_overlay = RoiOverlay(self.root)  
        self.root.mainloop()

    def show_roi_box(self, x1, y1, x2, y2):
        if hasattr(self, 'roi_overlay'):
            self.root.after(0, lambda: self.roi_overlay.update_box(x1, y1, x2, y2))

    def hide_roi_box(self):
        if hasattr(self, 'roi_overlay'):
            self.root.after(0, lambda: self.roi_overlay.hide())

    def timer_loop(self):
        if config["is_running"]:
            self.run_time_seconds += 1
            m, s = divmod(self.run_time_seconds, 60)
            h, m = divmod(m, 60)
            time_str = f"{h:02d}:{m:02d}:{s:02d}"
            if hasattr(self, 'overlay'):
                self.overlay.update_timer_data(time_str)
                
        self.root.after(1000, self.timer_loop)

    def log(self, text):
        def append_log():
            self.log_box.insert("end", text + "\n")
            self.log_box.see("end")
        self.root.after(0, append_log)
        
    def update_status(self, text, color="white"):
        if hasattr(self, 'overlay'):
            self.root.after(0, lambda: self.overlay.update_status_data(text, color))
        
    def update_stats(self):
        if hasattr(self, 'overlay'):
            self.root.after(0, lambda: self.overlay.update_stats_data(
                config['count_fish'], 
                config['count_big'], 
                config['count_keep'], 
                config['count_weixiu'],
                config['count_mutant']
            ))
        
    def start_script(self):
        if not config["roi"]:
            messagebox.showwarning("警告", "請先完成框選範圍")
            return
            
        self.overlay.root.deiconify()
        
        config["count_paogan_fail"] = 0
        config["is_running"] = True
        config["current_state"] = STATE_WAIT_FISH
        config["need_roi_recal"] = True  
        self.update_status("狀態：等待驚嘆號", "green")
        self.log("▶ 腳本已啟動，準備進行首次尋標校正")

    def stop_script(self):
        config["is_running"] = False
        self.update_status("狀態：已停止", "gray")
        self.log("⏸ 腳本已暫停")

    def toggle_script(self):
        if config["is_running"]:
            self.stop_script()
        else:
            self.start_script()

    def start_roi_selection(self):
        self.root.iconify() 
        time.sleep(0.3)
        SelectionOverlay(self.hwnd, self.roi_selected_callback, mode="roi_fixed")

    def roi_selected_callback(self, coords):
        config["roi"] = coords
        save_config()  
        self.lbl_roi.configure(text=f"已框選範圍: {coords}", text_color="green")
        self.log(f"已更新並儲存框選範圍: {coords}")
        self.root.deiconify() 

    def start_pos_selection(self):
        self.root.iconify()
        time.sleep(0.3)
        SelectionOverlay(self.hwnd, self.pos_selected_callback, mode="point")

    def pos_selected_callback(self, coords):
        config["click_x"], config["click_y"] = coords
        save_config()  
        self.lbl_pos.configure(text=f"收竿位置: (X={coords[0]}, Y={coords[1]})", text_color="green")
        self.log(f"已更新並儲存收竿位置: {coords}")
        self.root.deiconify()

    def start_fish_roi_selection(self):
        self.root.iconify() 
        time.sleep(0.3)
        SelectionOverlay(self.hwnd, self.fish_roi_selected_callback, mode="roi_free")

    def fish_roi_selected_callback(self, coords):
        config["fish_roi"] = coords
        save_config()  
        self.lbl_fish_roi.configure(text=f"已設定魚類範圍", text_color="green")
        self.log(f"已更新並儲存魚類判斷範圍: {coords}")
        self.root.deiconify() 

    def start_mutant_roi_selection(self):
        self.root.iconify() 
        time.sleep(0.3)
        SelectionOverlay(self.hwnd, self.mutant_roi_selected_callback, mode="roi_free")

    def mutant_roi_selected_callback(self, coords):
        config["mutant_roi"] = coords
        save_config()  
        self.lbl_mutant_roi.configure(text=f"已設定變異範圍", text_color="green")
        self.log(f"已更新並儲存變異判斷範圍: {coords}")
        self.root.deiconify() 

    def toggle_quick_sell(self):
        config["quick_sell"] = self.chk_quick_sell_var.get()
        save_config()  
        status = "開啟" if config["quick_sell"] else "關閉"
        self.log(f"💰 快速賣魚模式已{status}")

    def toggle_roi_recal(self):
        config["enable_roi_recal"] = self.chk_roi_recal_var.get()
        save_config()  
        status = "開啟" if config["enable_roi_recal"] else "關閉"
        self.log(f"🔄 尋標校正功能已{status}")

    def bind_new_hotkey(self):
        try:
            keyboard.remove_all_hotkeys()
            new_key = self.entry_hotkey.get().strip().lower()
            new_drag_key = self.entry_drag_hotkey.get().strip().lower()
            new_webhook = self.entry_webhook.get().strip()
            
            config["hotkey"] = new_key
            config["drag_hotkey"] = new_drag_key
            config["webhook_url"] = new_webhook
            
            save_config()
            
            keyboard.add_hotkey(new_key, self.toggle_script)
            if hasattr(self, 'overlay'):
                keyboard.add_hotkey(new_drag_key, self.overlay.toggle_mode_safe)
                
            self.log(f"設定已儲存！快捷鍵/Webhook 更新完畢")
            messagebox.showinfo("成功", f"設定已成功儲存與套用！")
        except Exception as e:
            self.log(f"[錯誤] 設定儲存失敗: {e}")
            messagebox.showerror("錯誤", f"設定失敗: {e}")

# ==================== 啟動器呼叫入口 ====================
def start_bot(model_instance, hwnd_instance):
    global model, hwnd
    model = model_instance
    hwnd = hwnd_instance
    
    if hwnd is None:
        import tkinter as tk
        from tkinter import messagebox
        import sys
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showwarning("貼心提醒", "未開啟天天玩樂園", parent=root)
        sys.exit(1)

    # 啟動主控制 GUI
    FishingGUI(model, hwnd)