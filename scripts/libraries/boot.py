# /flash/boot.py
import pyb, os

# 等 /flash 掛載好
for _ in range(200):
    try:
        if 'flash' in os.listdir('/'):
            break
    except:
        pass
    pyb.delay(10)

# 交給 main.py
pyb.main('/flash/main.py')
