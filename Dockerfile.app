# Assignment 3: one image for an application replica (HTTP + gRPC in one process).
# docker-compose.yml runs three of these (deploy.replicas: 3) behind Envoy.
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt requirements-grpc.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-grpc.txt

# Same generation command as the README / Dockerfile.grpc
COPY proto ./proto
RUN mkdir -p grpc_gen data \
 && python -m grpc_tools.protoc -I proto --python_out=grpc_gen --grpc_python_out=grpc_gen proto/petstore.proto

COPY app.py database.py metrics.py grpc_server.py replica_identity.py run_replica.py ./

EXPOSE 5000 50051

CMD ["python", "run_replica.py"]
