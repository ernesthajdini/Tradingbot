Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\Admin\Desktop\Work\BBM\Claude code\TradingAgent"
WshShell.Run "python run.py auto start --live", 0, False
