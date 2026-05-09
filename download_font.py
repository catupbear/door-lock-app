import urllib.request
url = (
    "https://github.com/googlefonts/noto-cjk/raw/main"
    "/Sans/SubsetOTF/SC/NotoSansSC-Regular.otf"
)
print("正在下载字体...")
urllib.request.urlretrieve(url, "chinese_font.ttf")
print("下载完成！可以运行 python3 main.py 了")
