import os
import sys
import streamlit as st
from io import StringIO
from contextlib import contextmanager
import re
import base64
import time
import datetime
import json

# 修复路径并导入当前项目的模块
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__))))

from config import load_config
from and_controller import list_all_devices, AndroidController, traverse_tree
from model import parse_explore_rsp, parse_grid_rsp, OpenAIModel, QwenModel
from utils import print_with_color, draw_bbox_multi, draw_grid
import prompts


def get_image_base64(image_path):
    try:
        with open(image_path, "rb") as img_file:
            return base64.b64encode(img_file.read()).decode()
    except Exception as e:
        st.error(f"Error reading image {image_path}: {e}")
        return ""


def execute_task(task_desc, privacy_protection=False):
    """
    使用 subprocess 调用 task_executor.py 来执行任务
    """
    import subprocess
    import tempfile
    import glob
    
    print_with_color(f"Starting task execution: {task_desc}", "blue")
    if privacy_protection:
        print_with_color("Privacy protection mode enabled", "cyan")
    
    # 创建临时文件来传递任务描述
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write(task_desc)
        temp_file = f.name
    
    try:
        # 获取执行前的任务目录状态
        tasks_dir = "./tasks"
        if not os.path.exists(tasks_dir):
            os.makedirs(tasks_dir)
        
        before_dirs = set(os.listdir(tasks_dir)) if os.path.exists(tasks_dir) else set()
        
        # 设置环境变量来控制隐私保护
        env = os.environ.copy()
        if privacy_protection:
            env['PRIVACY_PROTECTION'] = 'true'
        else:
            env['PRIVACY_PROTECTION'] = 'false'
        
        # 使用 subprocess 调用 task_executor，传入任务描述
        process = subprocess.Popen(
            ["python3", "scripts/task_executor.py", "--app", "general"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=os.path.abspath("."),
            env=env
        )
        
        # 发送任务描述到子进程
        stdout, _ = process.communicate(input=task_desc + "\n")
        
        # 在侧边栏显示输出
        if stdout:
            for line in stdout.split('\n'):
                if line.strip():
                    print(line)  # 这会被 capture_and_stream 捕获
        
        # 查找新生成的任务目录
        after_dirs = set(os.listdir(tasks_dir)) if os.path.exists(tasks_dir) else set()
        new_dirs = after_dirs - before_dirs
        
        final_screenshot = None
        if new_dirs:
            # 找到最新的任务目录
            latest_dir = max(new_dirs, key=lambda d: os.path.getctime(os.path.join(tasks_dir, d)))
            task_dir_path = os.path.join(tasks_dir, latest_dir)
            
            # 查找最后的标注截图
            labeled_images = glob.glob(os.path.join(task_dir_path, "*_labeled.png"))
            if labeled_images:
                # 按文件名排序，取最后一个
                labeled_images.sort()
                final_screenshot = labeled_images[-1]
            else:
                # 如果没有标注图片，找普通截图
                screenshots = glob.glob(os.path.join(task_dir_path, "*.png"))
                if screenshots:
                    screenshots.sort()
                    final_screenshot = screenshots[-1]
        
        # 根据返回码生成响应
        if process.returncode == 0:
            response = f"✅ **Task completed successfully!**\n\nTask: '{task_desc}'\n\nThe task was executed using AppAgent with real-time mobile device interaction."
            if final_screenshot and "privacy" in final_screenshot:
                response += "\n\n🔒 **Privacy protection activated** - Clicked unrelated content to mislead recommendation algorithms."
        else:
            response = f"❌ **Task execution failed**\n\nTask: '{task_desc}'\n\nProcess returned with code: {process.returncode}"
        
        return response, final_screenshot
        
    except Exception as e:
        error_response = f"❌ **Error executing task**\n\nTask: '{task_desc}'\n\nError: {str(e)}"
        return error_response, None
    finally:
        # 清理临时文件
        try:
            os.unlink(temp_file)
        except:
            pass


if 'submitted' not in st.session_state:
    st.session_state.submitted = False
if 'output_lines' not in st.session_state:
    st.session_state.output_lines = []
if 'running' not in st.session_state:
    st.session_state.running = False
if 'screenshot_path' not in st.session_state:
    st.session_state.screenshot_path = None
if 'main_response' not in st.session_state:
    st.session_state.main_response = ""

# add new  state to resent the agent's thinking process
if 'agent_status' not in st.session_state:
    st.session_state.agent_status = ""
if 'current_app' not in st.session_state:
    st.session_state.current_app = ""

if 'selected_example_query' not in st.session_state:
    st.session_state.selected_example_query = ""

if 'privacy_protection_enabled' not in st.session_state:
    st.session_state.privacy_protection_enabled = False


def submit_query():
    st.session_state.submitted = True

st.set_page_config(
    page_title="AppAgent | Mobile AI Assistant Demo",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        'Get Help': 'https://github.com/mnotgod96/AppAgent',
        'Report a bug': 'https://github.com/mnotgod96/AppAgent/issues',
        'About': "# AppAgent\nA novel LLM-based multimodal agent framework designed to operate smartphone applications."
    }
)

# 创建一个用于捕获输出的上下文管理器
@contextmanager
def capture_and_stream():
    # 在主栏目创建一个空容器
    main_container = st.container()
    
    # 检查是否已有log内容的占位符，如果没有就创建一个
    if 'log_placeholder' not in st.session_state:
        st.session_state.log_placeholder = st.sidebar.empty()
    
    sidebar_placeholder = st.session_state.log_placeholder

    # 正则表达式，用于匹配并移除 ANSI 转义码
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])') # delete the color code

    # 创建一个自定义的文件对象来捕获输出
    class StreamCapture:
        def __init__(self):
            # 如果已有log内容，保留它
            if 'log_content' not in st.session_state:
                st.session_state.log_content = ""
            self.text = st.session_state.log_content
            
        def write(self, text):
            clean_text = ansi_escape.sub('', text)
            
            clean_text = re.sub(r'[′`]', "'", clean_text)
            
            clean_text = re.sub(r'(\d+)\s*[\'′`]\s*', r'\1\'', clean_text)
            
            clean_text = re.sub(r'([a-zA-Z0-9])\s*<br>', r'\1', clean_text)
            
            if re.search(r'(<br>)?[a-zA-Z0-9](<br>)[a-zA-Z0-9](<br>)', clean_text):
                clean_text = re.sub(r'(<br>)?([a-zA-Z0-9])(<br>)', r'\2', clean_text)

            clean_text = clean_text.rstrip('\n') + ('\n' if clean_text.endswith('\n') else '')

            clean_text = re.sub(r'\n{2,}', '\n', clean_text)

            html_text = clean_text.replace('\n', '<br>')
            
            self.text += html_text
            st.session_state.log_content = self.text
            
            formatted_text = f"""
            <div class="process-log" style="font-family: 'Roboto Mono', monospace; font-size: 0.85rem; 
                                           background-color: rgba(255, 255, 255, 0.25); border-radius: 8px; 
                                           padding: 1rem; line-height: 1.5; color: #E0E0E0; 
                                           max-height: 75vh; overflow-y: auto;">
                {self.text}
                <div id='end-of-content'></div>
            </div>
            """
            
            sidebar_placeholder.markdown(formatted_text, unsafe_allow_html=True)
            
            # 然后单独注入自动滚动脚本
            st.sidebar.markdown(
                """
                <script>
                    // 自动滚动到底部
                    function scrollSidebarToBottom() {
                        try {
                            const sidebar = window.parent.document.querySelector("[data-testid='stSidebar']");
                            if(sidebar) {
                                // 直接滚动侧边栏
                                sidebar.scrollTop = sidebar.scrollHeight;
                                
                                // 查找并滚动所有可能的容器
                                const scrollContainers = sidebar.querySelectorAll("div[data-testid='stVerticalBlock'], div[data-testid='stVerticalBlockBorderWrapper']");
                                scrollContainers.forEach(container => {
                                    if(container.scrollHeight > container.clientHeight) {
                                        container.scrollTop = container.scrollHeight;
                                    }
                                });
                            }
                        } catch(e) {
                            console.error("Scroll error:", e);
                        }
                    }
                    
                    // 立即执行一次
                    scrollSidebarToBottom();
                    
                    // 延迟多次执行以确保滚动到底部
                    setTimeout(scrollSidebarToBottom, 300);
                    setTimeout(scrollSidebarToBottom, 600);
                    setTimeout(scrollSidebarToBottom, 1000);
                </script>
                """,
                unsafe_allow_html=True
            )
            
        def flush(self):
            pass
    
    stream_capture = StreamCapture()
    old_stdout = sys.stdout
    sys.stdout = stream_capture
    
    try:
        yield stream_capture, main_container
    finally:
        sys.stdout = old_stdout

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
      /* 全局主题颜色 */
    :root {
        --primary-color: #1a202c;          /* 深蓝灰 - 主色调 */
        --secondary-color: #4299e1;        /* 明亮蓝色 - 强调色 */
        --accent-color: #38b2ac;           /* 青绿色 - 特殊强调 */
        --success-color: #48bb78;          /* 成功绿色 */
        --warning-color: #ed8936;          /* 警告橙色 */
        --light-bg: #2e86c1;               /* 浅灰背景 */
        --dark-bg: #21618c;                /* 更明亮的侧边栏背景 */
        --card-bg: #ffffff;                /* 卡片背景 */
        --text-color: #2d3748;             /* 主要文本颜色 */
        --light-text: #a0aec0;             /* 浅色文本 */
        --border-color: #e2e8f0;           /* 边框颜色 */
        --border-radius: 12px;             /* 统一边框圆角 */
        --box-shadow: 0 10px 25px rgba(0, 0, 0, 0.1), 0 6px 10px rgba(0, 0, 0, 0.05); /* 精致阴影 */
        --transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); /* 平滑过渡 */
    }
    
    /* 全局样式重置 */
    * {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    }
    
    /* 页面布局 */
    .main .block-container {
        max-width: 1400px;
        padding: 2rem 2rem;
        margin: 0 auto;
    }
    
    /* 隐藏Streamlit默认的header和footer */
    header[data-testid="stHeader"] {
        height: 0;
    }
    
    .stApp > footer {
        display: none;
    }
    
    /* Hero Section样式 */
    .hero-section {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        padding: 3rem 2rem;
        border-radius: var(--border-radius);
        margin-bottom: 3rem;
        text-align: center;
        position: relative;
        overflow: hidden;
    }
    
    .hero-section::before {
        content: '';
        position: absolute;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        background: url('data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs><pattern id="grain" width="100" height="100" patternUnits="userSpaceOnUse"><circle cx="50" cy="50" r="1" fill="white" opacity="0.1"/></pattern></defs><rect width="100" height="100" fill="url(%23grain)"/></svg>');
        pointer-events: none;
    }
    
    .hero-content {
        position: relative;
        z-index: 1;
    }
    
    .hero-title {
        font-size: 3.5rem;
        font-weight: 700;
        margin-bottom: 1rem;
        letter-spacing: -2px;
        text-shadow: 0 2px 4px rgba(0,0,0,0.3);
    }
    
    .hero-subtitle {
        font-size: 1.4rem;
        font-weight: 400;
        opacity: 0.9;
        margin-bottom: 2rem;
        max-width: 600px;
        margin-left: auto;
        margin-right: auto;
    }
    
    .cikm-badge {
        display: inline-block;
        background: rgba(255, 255, 255, 0.2);
        backdrop-filter: blur(10px);
        padding: 0.5rem 1.5rem;
        border-radius: 25px;
        font-weight: 500;
        font-size: 0.9rem;
        border: 1px solid rgba(255, 255, 255, 0.3);
    }
    
    /* 特色功能卡片 */
    .feature-cards {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 1rem;
        margin: 2rem 0;
    }
    
    .feature-card {
        background: var(--card-bg);
        border-radius: var(--border-radius);
        padding: 1.5rem;
        box-shadow: var(--box-shadow);
        border: 1px solid var(--border-color);
        transition: var(--transition);
        position: relative;
        overflow: hidden;
        min-width: 0;
        cursor: pointer;
    }
    
    .feature-card:hover {
        transform: translateY(-3px);
        box-shadow: 0 15px 30px rgba(0, 0, 0, 0.12);
        border-color: var(--secondary-color);
    }
    
    .feature-card:active {
        transform: translateY(-1px);
        box-shadow: 0 8px 20px rgba(0, 0, 0, 0.15);
    }
    
    .feature-icon {
        width: 50px;
        height: 50px;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 1.5rem;
        margin-bottom: 1rem;
        background: linear-gradient(135deg, var(--secondary-color), var(--accent-color));
        color: white;
    }
    
    .feature-title {
        font-size: 1.1rem;
        font-weight: 600;
        color: var(--text-color);
        margin-bottom: 0.6rem;
        line-height: 1.3;
    }
    
    .feature-description {
        color: var(--light-text);
        line-height: 1.5;
        font-size: 0.85rem;
    }
    
    /* 输入框优化 */
    div.stTextInput > div > div > input {
        width: 100% !important;
        padding: 1.2rem 1.5rem !important; /* 稍微增加padding */
        border-radius: var(--border-radius) !important;
        border: 2px solid var(--border-color) !important;
        font-size: 1.1rem !important;
        transition: var(--transition) !important;
        background: var(--card-bg) !important;
        box-shadow: 0 2px 8px rgba(0,0,0,0.05) !important; /* 增强阴影 */
    }
    
    div.stTextInput > div > div > input:focus {
        border-color: var(--secondary-color) !important;
        box-shadow: 0 0 0 3px rgba(66, 153, 225, 0.1) !important;
        outline: none !important;
    }
    
    /* 输入框和按钮区域的背景 */
    .input-controls-area {
        background: var(--card-bg);
        border-radius: var(--border-radius);
        padding: 1.5rem;
        margin: 1rem 0 2rem 0;
        box-shadow: 0 4px 6px rgba(0,0,0,0.05);
        border: 1px solid var(--border-color);
    }
    
    /* 按钮样式优化 */
    .stButton > button {
        background: linear-gradient(135deg, var(--secondary-color), var(--accent-color)) !important;
        color: white !important;
        font-weight: 600 !important;
        border: none !important;
        border-radius: var(--border-radius) !important;
        padding: 1rem 2rem !important;
        font-size: 1.1rem !important;
        letter-spacing: 0.5px !important;
        height: auto !important;
        transition: var(--transition) !important;
        box-shadow: 0 4px 15px rgba(66, 153, 225, 0.4) !important;
        text-transform: none !important;
        min-height: 3.2rem !important;
    }
    
    .stButton > button:hover {
        transform: translateY(-2px) !important;
        box-shadow: 0 8px 25px rgba(66, 153, 225, 0.5) !important;
    }
    
    .stButton > button:active {
        transform: translateY(0) !important;
    }
    
    /* 响应容器样式 */
    .response-container {
        background: var(--card-bg);
        border-radius: var(--border-radius);
        box-shadow: var(--box-shadow);
        padding: 2rem;
        margin: 2rem 0;
        border: 1px solid var(--border-color);
    }
    
    .result-header {
        display: flex;
        align-items: center;
        margin-bottom: 1.5rem;
        padding-bottom: 1rem;
        border-bottom: 1px solid var(--border-color);
    }
    
    .result-icon {
        width: 40px;
        height: 40px;
        border-radius: 50%;
        background: linear-gradient(135deg, var(--success-color), #38b2ac);
        display: flex;
        align-items: center;
        justify-content: center;
        margin-right: 1rem;
        color: white;
        font-size: 1.2rem;
    }
    
    .screenshot-container {
        background: var(--light-bg);
        border-radius: var(--border-radius);
        padding: 2rem;
        margin-top: 2rem;
        border: 1px solid var(--border-color);
    }
    
    .screenshot-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
        gap: 1.5rem;
        margin-top: 1.5rem;
    }
    
    .screenshot-item {
        background: white;
        border-radius: var(--border-radius);
        padding: 1rem;
        box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        border: 1px solid var(--border-color);
    }
    
    /* 添加截图尺寸限制 */
    .screenshot-item img {
        max-height: 500px;
        object-fit: contain;
        margin: 0 auto;
        display: block;
    }
    
    /* 单张截图的尺寸限制 */
    .single-screenshot {
        max-height: 600px;
        object-fit: contain;
        margin: 0 auto;
        display: block;
        border-radius: 8px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.1);
    }
    
    .app-badge {
        display: inline-block;
        background: var(--secondary-color);
        color: white;
        padding: 0.3rem 0.8rem;
        border-radius: 15px;
        font-size: 0.8rem;
        font-weight: 500;
        margin-bottom: 0.8rem;
    }
    
    /* 侧边栏样式 */
    [data-testid="stSidebar"] {
        background: var(--dark-bg);
        padding-top: 2rem;
    }
    
    [data-testid="stSidebar"] [data-testid="stMarkdown"] {
        color: #e2e8f0;
    }
    
    .sidebar-title {
        color: white !important;
        font-weight: 700 !important;
        font-size: 1.4rem !important;
        margin-bottom: 1.5rem !important;
        padding-bottom: 0.75rem !important;
        border-bottom: 2px solid rgba(255, 255, 255, 0.2) !important;
    }    .sidebar-info {
        background: rgba(255, 255, 255, 0.25);
        border-radius: var(--border-radius);
        padding: 1.5rem;
        margin-bottom: 1.5rem;
        backdrop-filter: blur(10px);
        border: 1px solid rgba(255, 255, 255, 0.35);
    }.process-log {
        background: rgba(255, 255, 255, 0.25);
        border-radius: var(--border-radius);
        padding: 1rem;
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.85rem;
        color: #e2e8f0;
        max-height: 70vh;
        overflow-y: auto;
        line-height: 1.5;
        margin-top: 1rem;
        border: 1px solid rgba(255, 255, 255, 0.35);
    }
    
    /* 状态指示器 */
    .status-indicator {
        background: linear-gradient(135deg, #f0f9ff, #e6f7ff);
        border-radius: var(--border-radius);
        padding: 1.5rem;
        margin: 1.5rem 0;
        display: flex;
        align-items: center;
        border: 1px solid rgba(66, 153, 225, 0.2);
        box-shadow: 0 4px 6px rgba(0,0,0,0.05);
    }
    
    .spinner {
        width: 28px;
        height: 28px;
        border: 3px solid rgba(66, 153, 225, 0.1);
        border-radius: 50%;
        border-top-color: var(--secondary-color);
        animation: spin 1s linear infinite;
        margin-right: 15px;
    }
    
    @keyframes spin {
        0% { transform: rotate(0deg); }
        100% { transform: rotate(360deg); }
    }
    
    .status-text {
        color: var(--secondary-color);
        font-weight: 600;
        font-size: 1.1rem;
    }
    
    /* 底部信息 */
    .footer-info {
        margin-top: 4rem;
        padding: 2rem;
        background: var(--light-bg);
        border-radius: var(--border-radius);
        text-align: center;
        border: 1px solid var(--border-color);
    }
    
    .citation-box {
        background: white;
        border-radius: 8px;
        padding: 1.5rem;
        margin-top: 1rem;
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.9rem;
        border: 1px solid var(--border-color);
        text-align: left;
        color: var(--text-color);
    }
    
    /* 响应式设计 */
    @media (max-width: 1200px) {
        .feature-cards {
            grid-template-columns: repeat(3, 1fr);
            gap: 0.8rem; /* 进一步减小间距 */
        }
        
        .feature-card {
            padding: 1.2rem; /* 在中等屏幕上进一步减小padding */
        }
        
        .feature-title {
            font-size: 1rem; /* 稍微减小标题字体 */
        }
        
        .feature-description {
            font-size: 0.8rem; /* 稍微减小描述字体 */
        }
    }
    
    @media (max-width: 900px) {
        .feature-cards {
            grid-template-columns: repeat(2, 1fr);
        }
        
        .input-title {
            font-size: 1.6rem;
        }
        
        .input-controls-area {
            padding: 1.2rem;
        }
    }
    
    @media (max-width: 768px) {
        .hero-title {
            font-size: 2.5rem;
        }
        
        .feature-cards {
            grid-template-columns: 1fr;
        }
        
        .feature-card {
            padding: 1rem; /* 移动设备上最小padding */
        }
        
        .input-title {
            font-size: 1.4rem;
        }
        
        .input-controls-area {
            padding: 1rem;
            margin: 0.5rem 0 1.5rem 0;
        }
        
        .example-queries {
            flex-direction: column;
            align-items: center;
            padding: 0 0.5rem;
        }
        
        .example-queries .stButton {
            width: 100%;
            max-width: 280px; /* 稍微减小最大宽度 */
        }
        
        .main .block-container {
            padding: 1rem;
        }
        
        /* 移动设备上的截图优化 */
        .screenshot-grid {
            grid-template-columns: 1fr;
        }
        
        .screenshot-item img, .single-screenshot {
            max-height: 350px;
        }
    }
    
    /* 示例查询按钮 */
    .example-queries {
        display: flex;
        flex-wrap: wrap;
        gap: 0.8rem;
        justify-content: center;
        margin-bottom: 2rem;
        padding: 0 1rem; /* 添加左右padding */
    }
    
    .example-query {
        background: var(--light-bg);
        border: 1px solid var(--border-color);
        border-radius: 20px;
        padding: 0.6rem 1.2rem;
        font-size: 0.85rem;
        color: var(--text-color);
        cursor: pointer;
        transition: var(--transition);
        white-space: nowrap;
    }
    
    .example-query:hover {
        background: var(--secondary-color);
        color: white;
        transform: translateY(-1px);
    }
    
    /* 重新样式化示例查询的Streamlit按钮 */
    .example-queries .stButton > button {
        background: var(--light-bg) !important;
        border: 1px solid var(--border-color) !important;
        border-radius: 25px !important; /* 稍微增加圆角 */
        padding: 0.7rem 1.5rem !important; /* 稍微增加padding */
        font-size: 0.9rem !important; /* 稍微增大字体 */
        color: var(--text-color) !important;
        transition: var(--transition) !important;
        white-space: nowrap !important;
        height: auto !important;
        min-height: auto !important;
        font-weight: 500 !important; /* 稍微增加字重 */
        text-transform: none !important;
        letter-spacing: normal !important;
        box-shadow: 0 2px 4px rgba(0,0,0,0.05) !important; /* 添加轻微阴影 */
    }
    
    .example-queries .stButton > button:hover {
        background: var(--secondary-color) !important;
        color: white !important;
        transform: translateY(-2px) !important; /* 增加悬停移动距离 */
        border-color: var(--secondary-color) !important;
        box-shadow: 0 4px 12px rgba(66, 153, 225, 0.3) !important; /* 增强悬停阴影 */
    }
    
    .example-queries .stButton > button:active {
        transform: translateY(0) !important;
    }
</style>
""", unsafe_allow_html=True)

# 主要内容区
main_col = st.container()

with main_col:
    st.markdown("""
    <div class="hero-section">
        <div class="hero-content">
            <h1 class="hero-title">AppAgent</h1>
            <p class="hero-subtitle">
                A novel LLM-based multimodal agent framework designed to operate smartphone applications
            </p>
        </div>
    </div>
    """, unsafe_allow_html=True)
    
    st.markdown("""
    <div class="feature-cards">
        <div class="feature-card">
            <div class="feature-icon">📱</div>
            <div class="feature-title">Mobile App Control</div>
            <div class="feature-description">
                Direct interaction with mobile applications through visual understanding and touch automation.
            </div>
        </div>
        <div class="feature-card">
            <div class="feature-icon">🧠</div>
            <div class="feature-title">Multimodal Understanding</div>
            <div class="feature-description">
                Combines visual perception with natural language processing to understand and execute complex tasks.
            </div>
        </div>
        <div class="feature-card">
            <div class="feature-icon">🎯</div>
            <div class="feature-title">Task Automation</div>
            <div class="feature-description">
                Learns app interfaces and automates complex workflows through step-by-step interaction planning.
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)
    
    st.markdown("""
        <h2 style="font-size: 1.8rem; font-weight: 600; color: var(--text-color); 
                   margin-bottom: 0.8rem; text-align: center;">Try AppAgent</h2>
        <p style="color: var(--light-text); text-align: center; margin-bottom: 1.5rem; 
                  font-size: 1rem; max-width: 600px; margin-left: auto; margin-right: auto;">
           Enter your task description and watch how the agent interacts with mobile applications
        </p>
    """, unsafe_allow_html=True)
    
    st.markdown('<div class="example-queries">', unsafe_allow_html=True)
    col1, col2, col3 = st.columns(3, gap="medium")
    
    with col1:
        if st.button("📧 Send email to contact", key="example1", use_container_width=True):
            st.session_state.selected_example_query = "Send an email to my main contact"
            st.rerun()
    
    with col2:
        if st.button("� Take screenshot and share", key="example2", use_container_width=True):
            st.session_state.selected_example_query = "Take a screenshot and share it via messaging app"
            st.rerun()
    
    with col3:
        if st.button("🎵 Play music from playlist", key="example3", use_container_width=True):
            st.session_state.selected_example_query = "Open music app and play my favorite playlist"
            st.rerun()
    
    st.markdown('</div>', unsafe_allow_html=True)
    
    st.markdown('<div class="input-controls-area">', unsafe_allow_html=True)
    input_col, button_col = st.columns([4, 1], gap="medium")
    with input_col:
        default_value = st.session_state.selected_example_query if st.session_state.selected_example_query else ""
        query = st.text_input(
            "",
            value=default_value,
            key="query_input",
            on_change=submit_query,
            placeholder="Describe the mobile task you want to perform (e.g., 'Send an email to my main contact')",
            label_visibility="collapsed"
        )
        
        # 隐私保护选项
        st.session_state.privacy_protection_enabled = st.checkbox(
            "🔒 Enable Privacy Protection",
            value=st.session_state.privacy_protection_enabled,
            help="After completing the task, the agent will automatically click unrelated content to mislead recommendation algorithms and protect your privacy."
        )
        
    with button_col:
        st.markdown("<div style='height: 0.5rem;'></div>", unsafe_allow_html=True)  # 减少间距
        run_clicked = st.button("🚀 Execute", use_container_width=True)
    
    st.markdown('</div>', unsafe_allow_html=True)

    response_area = st.empty()
    screenshot_area = st.empty()

st.sidebar.markdown('<h2 class="sidebar-title">🔍 Execution Monitor</h2>', unsafe_allow_html=True)
st.sidebar.markdown("""
<div class="sidebar-info">
    <h4 style="color: white; margin-top: 0; margin-bottom: 1rem;">Real-time Process</h4>
    <p style="color: #e2e8f0; margin: 0; font-size: 0.9rem; line-height: 1.6;">
        This panel shows the agent's decision-making process in real-time. Watch how it:
    </p>
    <ul style="color: #e2e8f0; font-size: 0.85rem; line-height: 1.5; margin-top: 0.8rem; padding-left: 1.2rem;">
        <li>Analyzes your task requirements</li>
        <li>Navigates mobile applications</li>
        <li>Executes precise interactions</li>
        <li>Protects your privacy (optional)</li>
    </ul>
</div>
""", unsafe_allow_html=True)

st.sidebar.markdown("""
<div style="background: rgba(255, 255, 255, 0.08); border-radius: 8px; padding: 1rem; margin-bottom: 1rem; border: 1px solid rgba(255, 255, 255, 0.15);">
    <h5 style="color: white; margin-top: 0; margin-bottom: 0.8rem;">Agent Capabilities</h5>
    <div style="display: flex; flex-direction: column; gap: 0.5rem;">
        <div style="display: flex; align-items: center; color: #e2e8f0; font-size: 0.85rem;">
            <span style="color: #4CAF50; margin-right: 8px;">✓</span> Tap & Click
        </div>
        <div style="display: flex; align-items: center; color: #e2e8f0; font-size: 0.85rem;">
            <span style="color: #2196F3; margin-right: 8px;">✓</span> Text Input
        </div>
        <div style="display: flex; align-items: center; color: #e2e8f0; font-size: 0.85rem;">
            <span style="color: #FF9800; margin-right: 8px;">👆</span> Long Press
        </div>
        <div style="display: flex; align-items: center; color: #e2e8f0; font-size: 0.85rem;">
            <span style="color: #9C27B0; margin-right: 8px;">↔️</span> Swipe Gestures
        </div>
        <div style="display: flex; align-items: center; color: #a0aec0; font-size: 0.85rem;">
            <span style="color: #607D8B; margin-right: 8px;">📱</span> Any Android App
        </div>
    </div>
</div>
""", unsafe_allow_html=True)

# 动态显示隐私保护状态
privacy_status = "Enabled" if st.session_state.privacy_protection_enabled else "Disabled"
privacy_color = "#4CAF50" if st.session_state.privacy_protection_enabled else "#FF5722"
privacy_icon = "✓" if st.session_state.privacy_protection_enabled else "✗"

st.sidebar.markdown(f"""
<div style="background: rgba(255, 255, 255, 0.08); border-radius: 8px; padding: 1rem; margin-bottom: 1rem; border: 1px solid rgba(255, 255, 255, 0.15);">
    <h5 style="color: white; margin-top: 0; margin-bottom: 0.8rem;">🔒 Privacy Protection</h5>
    <div style="display: flex; flex-direction: column; gap: 0.5rem;">
        <div style="display: flex; align-items: center; color: #e2e8f0; font-size: 0.85rem;">
            <span style="color: {privacy_color}; margin-right: 8px;">{privacy_icon}</span> {privacy_status}
        </div>
        <div style="display: flex; align-items: center; color: #e2e8f0; font-size: 0.85rem;">
            <span style="color: #FF5722; margin-right: 8px;">🎯</span> Misleads algorithms
        </div>
        <div style="display: flex; align-items: center; color: #e2e8f0; font-size: 0.85rem;">
            <span style="color: #9C27B0; margin-right: 8px;">🛡️</span> Protects privacy
        </div>
        <p style="color: #a0aec0; font-size: 0.8rem; margin: 0.5rem 0 0 0; line-height: 1.4;">
            {('When enabled, the agent will click unrelated content after completing your task to confuse recommendation systems.' if st.session_state.privacy_protection_enabled else 'Enable the checkbox above to activate privacy protection mode.')}
        </p>
    </div>
</div>
""", unsafe_allow_html=True)

if run_clicked or st.session_state.submitted:
    if st.session_state.submitted:
        st.session_state.submitted = False
    
    final_query = query.strip() if query else ""
    
    if not final_query:
        st.error("Please enter a task description or select one of the example queries above.")
        st.stop()
    
    st.info(f"🎯 **Executing Task:** {final_query}")
    
    response_area.empty()
    screenshot_area.empty()
    
    if 'log_content' in st.session_state:
        st.session_state.log_content = ""
    
    st.session_state.agent_status = "thinking"
    st.session_state.current_app = ""  # reset current app
    

    if 'status_placeholder' not in st.session_state:
        st.session_state.status_placeholder = st.empty()
    else:
        st.session_state.status_placeholder.empty()
        st.session_state.status_placeholder = st.empty()
    
    with st.session_state.status_placeholder.container():
        st.markdown("""
        <div class="status-indicator">
            <div class="spinner"></div>
            <span class="status-text">🧠 Agent is analyzing your task request...</span>
        </div>
        """, unsafe_allow_html=True)
    
    
    # 使用本文件中定义的 execute_task 函数
    try:
        with capture_and_stream() as (output, main_container):
            main_response, screenshot_paths = execute_task(final_query, st.session_state.privacy_protection_enabled)
            st.session_state.main_response = main_response
            st.session_state.screenshot_path = screenshot_paths
    except Exception as e:
        st.error(f"Error: {e}")
        st.session_state.main_response = f"ERROR: {e}"
        st.session_state.screenshot_path = None
    
    with st.session_state.status_placeholder.container():
        st.markdown("""
        <div class="status-indicator" style="background: linear-gradient(135deg, #f1f8e9, #e8f5e8);">
            <div style="width: 28px; height: 28px; background: #48bb78; border-radius: 50%; display: flex; align-items: center; justify-content: center; margin-right: 15px; color: white; font-size: 16px;">✓</div>
            <span style="color: #48bb78; font-weight: 600; font-size: 1.1rem;">Task execution completed successfully!</span>
        </div>
        """, unsafe_allow_html=True)
    
    with response_area.container():
        st.markdown("""
        <div class="response-container">
            <div class="result-header">
                <div class="result-icon">✓</div>
                <div>
                    <h3 style="margin: 0; color: var(--text-color); font-size: 1.4rem;">Task Completed Successfully</h3>
                    <p style="margin: 0; color: var(--light-text); font-size: 0.9rem;">Enhanced response with mobile app integration</p>
                </div>
            </div>
        """, unsafe_allow_html=True)
        st.markdown(st.session_state.main_response)
        st.markdown("</div>", unsafe_allow_html=True)    # 显示最终截图
    if st.session_state.screenshot_path:
        with screenshot_area.container():
            st.markdown("""
            <div class="screenshot-container">
                <div style="display: flex; align-items: center; margin-bottom: 1.5rem;">
                    <div style="width: 40px; height: 40px; border-radius: 50%; background: linear-gradient(135deg, var(--secondary-color), var(--accent-color)); 
                             display: flex; align-items: center; justify-content: center; margin-right: 1rem; color: white; font-size: 1.2rem;">📱</div>
                    <div>
                        <h3 style="margin: 0; color: var(--text-color); font-size: 1.4rem;">Mobile App Screenshot</h3>
                        <p style="margin: 0; color: var(--light-text); font-size: 0.9rem;">Final state after task execution</p>
                    </div>
                </div>
            """, unsafe_allow_html=True)
            
            path = st.session_state.screenshot_path
            if path and os.path.exists(path):
                st.markdown(f"""
                <div class="screenshot-item">
                    <img src="data:image/png;base64,{get_image_base64(path)}" class="single-screenshot" alt="Mobile App Screenshot">
                </div>
                """, unsafe_allow_html=True)
            elif path:
                st.info(f"Screenshot path: {path}")
            
            st.markdown("</div>", unsafe_allow_html=True)
            
    st.markdown("""
    <div style="margin-top: 3rem; padding-top: 1.5rem; border-top: 1px solid #e1e4e8; text-align: center;">
        <p style="font-size: 0.9rem; color: #6c757d;">
            <strong>AppAgent</strong> - Multimodal Agent Framework for Mobile Applications
        </p>
        <p style="font-size: 0.85rem; color: #6c757d; margin-top: 0.5rem;">
            A novel LLM-based framework designed to operate smartphone applications autonomously.
        </p>
    </div>
    """, unsafe_allow_html=True)