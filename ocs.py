import json
import http.server
import urllib.request
import os
import re

# ================== 用户自定义配置区 ==================
CONFIG = {
    "PORT": 8000,
    "DB_DIR": "libraries",       
    
    # 当前正在做的课程名字（不需要加.json后缀）
    "ACTIVE_COURSE": "智慧树-改革开放与新时代", 
    
    # 是否开启 AI 搜题功能 
    "ENABLE_AI": True, 
    
    # 填写兼容 OpenAI 格式的 API 接口信息 （DeepSeek/GPT/Gemini/Ollama等）
    "AI_API_KEY": "your-api-key-here",
    "AI_BASE_URL": "https://api.deepseek.com/v1/chat/completions",
    "AI_MODEL": "deepseek-chat", 
}
# =====================================================

# 获取当前活动数据库的文件路径
def get_db_path():
    db_dir = CONFIG.get("DB_DIR", "libraries")
    os.makedirs(db_dir, exist_ok=True)  # 自动创建题库文件夹
    return os.path.join(db_dir, f"{CONFIG.get('ACTIVE_COURSE', 'default')}.json")

ACTIVE_DB_PATH = get_db_path()

# 初始化加载当前课程的数据库
def load_db():
    if os.path.exists(ACTIVE_DB_PATH):
        with open(ACTIVE_DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

db = load_db()

def save_db():
    with open(ACTIVE_DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

class UniversalProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', '*')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.end_headers()

    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        input_data = json.loads(self.rfile.read(content_length).decode('utf-8'))
        
        title = input_data.get("title", "").strip()
        options = input_data.get("options", "").strip()
        q_type = input_data.get("type", "single")
        
        # --- 1. 解析选项，提取纯文本用于统一缓存 Key (解决网页选项打乱的问题) ---
        opts_dict = {}     # 字母到内容的映射，例 {'A': '苹果'}
        raw_lines = [o.strip() for o in options.split('\n') if o.strip()]
        
        current_letter = None
        for line in raw_lines:
            # 匹配 'A. 苹果' 或单独一行的 'A.'
            m = re.match(r'^([A-Z])[\.、\s]+(.*)$', line)
            if m:
                current_letter = m.group(1)
                text = m.group(2).strip()
                opts_dict[current_letter] = text
            else:
                # 说明这一行是具体文字。它被换行了，那就接在刚才的字母屁股后面
                if current_letter is not None:
                    opts_dict[current_letter] = (opts_dict.get(current_letter, "") + " " + line).strip()
        
        # 构建文本到字母的反向映射，并过滤掉空值
        reverse_opts = {v: k for k, v in opts_dict.items() if v}
        
        # 归一化 Key：提取文本排序后拼接，不管页面里是 A苹果 还是 B苹果，生成的 Key 都一样
        if reverse_opts:
            stable_opts = " | ".join(sorted(reverse_opts.keys()))
        else:
            # 如果没有检测到A/B/C等前缀（如判断题/纯文字选项），也进行排序防乱序
            stable_opts = " | ".join(sorted(raw_lines))
            
        storage_key = f"[{q_type}] {title} | {stable_opts}"

        answer = "未找到答案"
        
        # --- 2. 尝试从共享 JSON 中读取 ---
        db_hit_value = db.get(storage_key)
            
        if db_hit_value:
            print(f"✅ 命中题库: {title[:15]}... -> {db_hit_value}")
            
            if q_type == "judgement":
                # 判断题不需要复杂的选项逆向映射，直接返回库里的"对"或"错"或"A/B"，以防把正确文字错误映射掉
                answer = str(db_hit_value)
            else:
                # 题库里存的可能是内容(比如"苹果")，也可能是字母(比如"A")
                # 把它转换为 "当前用户的选项字母" 返回给插件
                final_ans = []
                for p in str(db_hit_value).split('#'):
                    p = p.strip()
                    if p:
                        final_ans.append(reverse_opts.get(p, p))
                
                # 多选题将字母按照 A#B 排序返回给客户端，保证返回格式标准
                answer = "#".join(sorted(final_ans) if q_type == "multiple" else final_ans)
        
        # --- 3. 如果没存过，且用户开启了 AI 功能 ---
        elif CONFIG["ENABLE_AI"] and CONFIG["AI_API_KEY"]:
            print(f"🤖 库中无记录，调取 AI ({CONFIG['AI_MODEL']})...")
            ai_answer = self.ask_ai(title, options, q_type)
            
            # 第一重校验：AI 请求成功，并且没有返回纯空格或纯井号
            is_valid_ans = ai_answer and ai_answer != "搜索失败" and ai_answer.replace("#", "").strip()
            
            if is_valid_ans:
                # 把 AI 答出的字母 (比如 "A#B") 转换成真正的文字存起来 ("苹果#香蕉")
                parts = [p.strip() for p in ai_answer.split('#') if p.strip()]
                # opts_dict.get(p) 如果找到的选项内容恰好为空（比如网页上真就只有 ABCD 不带文字），
                # 或者本来就不在 opts_dict 中，通过 `or p` 保底退回保留原始答案 p (例如 "A")
                storage_val = [opts_dict.get(p) or p for p in parts]
                final_store_val = "#".join(storage_val).strip()
                
                # 第二重校验：确保转换出来的文字确实有内容，不写入脏数据
                if final_store_val:
                    db[storage_key] = final_store_val
                    save_db()
                    answer = ai_answer
                    print(f"💾 已录入新题库！{title[:15]}... -> {final_store_val.replace(chr(10), ' ')}")
                else:
                    print(f"⚠️ 解析选项内容失败，文本为空，放弃写入: {ai_answer}")
            else:
                print(f"⚠️ AI 请求失败或未返回有效选项，放弃写入")
        else:
            print(f"❓ 库中无记录且未开启 AI")

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps({"answer": answer}).encode('utf-8'))

    def ask_ai(self, title, options, q_type):
        prompt = f"【{q_type}】\n题目：{title}\n选项：{options}"
        
        payload = {
            "model": CONFIG["AI_MODEL"],
            "messages": [
                {"role": "system", "content": "你是一个答题机器人。只回复答案内容，不输出解析。判断题回‘对/错’，多选题答案用#连接选项，如 A#B。"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.2
        }

        req = urllib.request.Request(
            CONFIG["AI_BASE_URL"],
            data=json.dumps(payload).encode('utf-8'),
            headers={"Authorization": f"Bearer {CONFIG['AI_API_KEY']}", "Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as f:
                res = json.loads(f.read().decode('utf-8'))
                raw = res['choices'][0]['message']['content']
                # 通用清洗逻辑
                ans = re.sub(r'\*+', '', raw) # 去 Markdown
                ans = re.sub(r'^(答案|结果|选择)[:：\s]*', '', ans) # 去前缀
                ans = ans.split('解析')[0].split('\n')[0].strip() # 截断解析和换行
                return re.sub(r'[,，\s]+', '#', ans) # 统一多选分隔符
        except Exception as e:
            print(f"❌ AI 故障: {e}")
            return "搜索失败"

if __name__ == "__main__":
    print(f"🌟 社区共享题库引擎已启动")
    print(f"📍 本地接口: http://localhost:{CONFIG['PORT']}/search")
    print(f"📚 当前加载课程: 【{CONFIG['ACTIVE_COURSE']}】")
    print(f"📂 数据库文件: {os.path.abspath(ACTIVE_DB_PATH)}")
    print(f"💡 AI 模式: {'开启' if CONFIG['ENABLE_AI'] else '关闭 (仅查库)'}")
    http.server.HTTPServer(('0.0.0.0', CONFIG["PORT"]), UniversalProxyHandler).serve_forever()