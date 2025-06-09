import os
import json
import requests
from io import BytesIO
from typing import List, Dict
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import uvicorn
import logging
import html

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === 微信公众号配置 ===
WECHAT_API_BASE_URL = "https://api.weixin.qq.com/cgi-bin/"

# === FastAPI 应用 ===
app = FastAPI()


# === 请求体模型 ===
class PublishRequest(BaseModel):
    title: str
    author: str
    content_html: str
    cover_image_url: str
    content_image_urls: List[str] = []
    APP_ID: str
    APP_SECRET: str


# === 工具函数 ===
def get_access_token(app_id: str, app_secret: str) -> str:
    """获取微信access_token"""
    url = f"{WECHAT_API_BASE_URL.rstrip('/')}/token?grant_type=client_credential&appid={app_id}&secret={app_secret}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if "access_token" in data:
            return data["access_token"]
        else:
            error_msg = f"获取access_token失败: {data}"
            logger.error(error_msg)
            raise Exception(error_msg)
    except Exception as e:
        error_msg = f"获取access_token时发生错误: {str(e)}"
        logger.error(error_msg)
        raise Exception(error_msg)


def download_image(url: str) -> BytesIO:
    """下载图片到内存"""
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return BytesIO(resp.content)
    except Exception as e:
        error_msg = f"下载图片失败: {url} - 错误: {str(e)}"
        logger.error(error_msg)
        raise Exception(error_msg)


def upload_image_to_wechat(access_token: str, image_bytes: BytesIO) -> str:
    """上传图片到微信素材库"""
    url = f"{WECHAT_API_BASE_URL.rstrip('/')}/material/add_material?access_token={access_token}&type=image"
    try:
        files = {'media': ('image.jpg', image_bytes, 'image/jpeg')}
        resp = requests.post(url, files=files, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if "media_id" in data:
            return data["media_id"]
        else:
            error_msg = f"上传图片失败: {data}"
            logger.error(error_msg)
            raise Exception(error_msg)
    except Exception as e:
        error_msg = f"上传图片时发生错误: {str(e)}"
        logger.error(error_msg)
        raise Exception(error_msg)


def process_content_images(content_html: str, image_urls: List[str], access_token: str) -> (str, Dict[str, str]):
    """处理内容中的图片"""
    media_ids = {}
    for idx, url in enumerate(image_urls):
        try:
            image_bytes = download_image(url)
            media_id = upload_image_to_wechat(access_token, image_bytes)
            placeholder = f"{{image{idx}}}"

            # 创建符合微信要求的图片标签
            wechat_img_tag = f'<img data-ratio="1.33" data-src="" data-type="jpeg" data-w="1280" src=""/>'

            # 替换占位符
            content_html = content_html.replace(placeholder, wechat_img_tag)
            media_ids[f"image{idx}"] = media_id
        except Exception as e:
            logger.error(f"处理图片{idx}时出错: {str(e)}")
            continue

    return content_html, media_ids


def create_draft(access_token: str, title: str, author: str, content_html: str, cover_media_id: str) -> str:
    """创建草稿"""
    url = f"{WECHAT_API_BASE_URL.rstrip('/')}/draft/add?access_token={access_token}"
    try:
        # 处理HTML特殊字符和编码
        processed_html = content_html.encode('utf-8').decode('unicode_escape')

        payload = {
            "articles": [{
                "title": title.encode('utf-8').decode('unicode_escape'),
                "author": author.encode('utf-8').decode('unicode_escape'),
                "content": processed_html,
                "thumb_media_id": cover_media_id,
                "need_open_comment": 0,
                "only_fans_can_comment": 0,
                "content_source_url": "https://openai.com"
            }]
        }
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if "media_id" in data:
            return data["media_id"]
        else:
            error_msg = f"创建草稿失败: {data}"
            logger.error(error_msg)
            raise Exception(error_msg)
    except Exception as e:
        error_msg = f"创建草稿时发生错误: {str(e)}"
        logger.error(error_msg)
        raise Exception(error_msg)


def publish_draft(access_token: str, draft_media_id: str) -> str:
    """发布草稿"""
    url = f"{WECHAT_API_BASE_URL.rstrip('/')}/freepublish/submit?access_token={access_token}"
    try:
        payload = {"media_id": draft_media_id}
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode") == 0:
            return data.get("publish_id", "发布成功")
        else:
            error_msg = f"发布失败: {data}"
            logger.error(error_msg)
            raise Exception(error_msg)
    except Exception as e:
        error_msg = f"发布草稿时发生错误: {str(e)}"
        logger.error(error_msg)
        raise Exception(error_msg)


# === 主接口 ===
@app.post("/publish")
async def publish_article(request: Request):
    try:
        # 解析请求体
        req_data = await request.json()
        logger.info(f"接收到的请求数据: {req_data}")

        # 验证请求数据
        required_fields = ['APP_ID', 'APP_SECRET', 'title', 'author', 'content_html', 'cover_image_url']
        if not all(key in req_data for key in required_fields):
            raise HTTPException(status_code=400, detail="缺少必要参数")

        # 获取access_token
        token = get_access_token(req_data['APP_ID'], req_data['APP_SECRET'])
        logger.info(f"成功获取access_token")

        # 上传封面图
        cover_bytes = download_image(req_data['cover_image_url'])
        cover_media_id = upload_image_to_wechat(token, cover_bytes)
        logger.info(f"封面图上传成功，media_id: {cover_media_id}")

        # 上传正文图并处理内容
        content_image_urls = req_data.get('content_image_urls', [])
        processed_html, content_media_ids = process_content_images(
            req_data['content_html'], content_image_urls, token
        )
        logger.info(f"正文图片处理完成，共上传 {len(content_media_ids)} 张图片")

        # 创建草稿
        draft_media_id = create_draft(
            token, req_data['title'], req_data['author'], processed_html, cover_media_id
        )
        logger.info(f"草稿创建成功，media_id: {draft_media_id}")

        # 自动发布
        publish_id = publish_draft(token, draft_media_id)
        logger.info(f"文章发布成功，publish_id: {publish_id}")

        return {
            "status": "success",
            "draft_media_id": draft_media_id,
            "cover_media_id": cover_media_id,
            "content_media_ids": content_media_ids,
            "publish_id": publish_id
        }

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"处理文章发布时发生错误: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# === 启动本地服务 ===
if __name__ == "__main__":
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
