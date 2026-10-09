import time, base64
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

options = webdriver.ChromeOptions()
options.add_argument("--kiosk")
driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
driver.get("https://www.uvu.edu")
time.sleep(5)

scroll_y = driver.execute_script("return window.scrollY;")
print(f"scrollY hiện tại: {scroll_y}")

# Lấy kích thước thật của toàn trang
page_width = driver.execute_script("return document.documentElement.scrollWidth")
page_height = driver.execute_script("return document.documentElement.scrollHeight")

# Dùng Chrome DevTools Protocol để chụp toàn trang
driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
    "width": page_width,
    "height": page_height,
    "deviceScaleFactor": 1,
    "mobile": False,
})
result = driver.execute_cdp_cmd("Page.captureScreenshot", {
    "captureBeyondViewport": True,
    "format": "png",
})
with open("test_fullpage.png", "wb") as f:
    f.write(base64.b64decode(result["data"]))

print(f"Đã lưu test_fullpage.png ({page_width}x{page_height})")

driver.quit()