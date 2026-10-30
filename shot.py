from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b=p.chromium.launch(executable_path="/usr/bin/google-chrome",args=["--no-sandbox","--use-gl=swiftshader","--enable-unsafe-swiftshader"])
    pg=b.new_page(viewport={"width":1400,"height":850}); errs=[]; pg.on("console",lambda m: m.type=="error" and errs.append(m.text))
    pg.goto("http://127.0.0.1:18123/index.html", wait_until="domcontentloaded"); print(pg.title()); pg.wait_for_timeout(5000)
    pg.evaluate("show(6)"); pg.wait_for_timeout(1500)
    pg.mouse.click(1000,500); pg.wait_for_timeout(1500)
    print("popup:",pg.inner_text(".maplibregl-popup-content") if pg.query_selector(".maplibregl-popup-content") else None, "errors:",errs)
    pg.screenshot(path="screenshot.png"); b.close()
