# Windows 批量量产工具

量产工具负责一次选择多块 ESP32-C3、并行烧录、读取固件自检结果，并把每台设备记录到 `factory/records/production.csv`。固件会通过 `ATI`、`AT+CGMM` 和能力探测自动适配 ML307A、ML307C、ML307Y，同时继续兼容上游 ML307R。

## 首次准备

1. 安装 Python 3 和 Arduino CLI。
2. 在仓库根目录执行：

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\factory\build_firmware.ps1
   ```

3. 双击 `factory/start.bat`。首次启动只会安装固定版本的 `pyserial`。

## 每批操作

1. 同时插入待烧录设备，点击“刷新串口”。工具默认选择 Espressif、CP210x、CH34x 等候选串口。
2. 保持“全片擦除”开启，按产品验收要求选择“要求 SIM 就绪”和“要求成功驻网”。
3. 点击“开始所选设备”。同一批最多并行处理 4 台；失败设备会保留原因，不影响其他设备。
4. 仅把显示 `PASS` 的设备作为良品。CSV 可直接由 Excel 打开，每台设备另有完整 JSON 记录。

全片擦除会清除旧 Wi-Fi、推送方式和短信记录，适合出厂烧录。返修时如需保留配置，可取消该选项。

## 命令行

```powershell
# 查看串口
.\factory\start.ps1 --list

# 量产所有自动识别到的 ESP32 串口，并要求 SIM 和网络通过
.\factory\start.ps1 --all --require-sim --require-network

# 只验收已经烧录的设备
.\factory\start.ps1 --ports COM3 COM4 --skip-flash --require-sim
```

量产判定读取 USB 上的 `FACTORY STATUS` 只读协议，不直接占用 ML307 UART。记录中保存 ESP32 芯片 ID、固件版本、完整模组料号/固件、SIM 类型、ICCID 尾号和 PLMN，不保存短信内容、Wi-Fi 密码或推送密钥。
