# Railway "s3" service (RustFS). Attach a volume at /data; public domain -> port 9000.
# Needs RUSTFS_ACCESS_KEY and RUSTFS_SECRET_KEY variables.
FROM rustfs/rustfs:1.0.6
USER root
ENV RUSTFS_VOLUMES=/data RUSTFS_ADDRESS=0.0.0.0:9000 RUSTFS_CONSOLE_ENABLE=false
EXPOSE 9000
