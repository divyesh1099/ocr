# Railway "dynamodb" service. Attach a volume at /data.
FROM amazon/dynamodb-local:3.3.2
USER root
EXPOSE 8000
CMD ["-jar", "DynamoDBLocal.jar", "-sharedDb", "-dbPath", "/data"]
