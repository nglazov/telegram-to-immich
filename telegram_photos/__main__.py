import uvicorn

if __name__ == "__main__":
    uvicorn.run("telegram_photos.app:app", host="127.0.0.1", port=8213, workers=1, access_log=False)
