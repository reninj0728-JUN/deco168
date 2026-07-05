# pytest 收集設定：
# 下面這些 test_/verify_ 開頭的檔案是「腳本式」（module-level 直接執行、
# 需要 API key、跑真網路呼叫、結尾 sys.exit），不是 pytest 測試——
# 收集時 import 會 INTERNALERROR 或誤觸外部呼叫，排除收集、保留手動執行用途。
# 真正的 pytest 測試放 test_catalog_and_match.py（無網路、秒級、可當 CI 基線）。
collect_ignore_glob = [
    "test_momo*.py",
    "test_ikea*.py",
]
collect_ignore = [
    "test_api.py",                  # smoke test，真呼叫 Gemini/FAL
    "test_full_pipeline.py",        # pipeline 核心程式（檔名歷史遺留）
    "test_anchored_regression.py",  # 腳本式回歸，需真 job 資料
    "test_furniture_match_fix.py",  # 腳本式
    "test_photo_meta_v1.py",        # 腳本式，結尾 sys.exit
    "test_photo_meta_v1_replay.py", # 腳本式
]
