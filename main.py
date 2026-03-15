import uvicorn

from api import create_app

app = create_app()

if __name__ == "__main__":
    # reload=False: the background worker thread must not be duplicated by
    # Uvicorn's hot-reload watchdog.  Use --reload only during front-end work.
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)

