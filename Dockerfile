FROM python:3.12-slim
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH USUAL_DATA_DIR=/tmp/usual PORT=7860
WORKDIR /home/user/app
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt
COPY --chown=user app ./app
EXPOSE 7860
CMD ["sh", "-c", "python -m uvicorn app.api:create_app --factory --host 0.0.0.0 --port ${PORT}"]
