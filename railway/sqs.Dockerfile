# Railway "sqs" service (ElasticMQ). Attach a volume at /data.
FROM softwaremill/elasticmq-native:1.7.1
USER root
COPY railway/elasticmq.conf /opt/elasticmq.conf
EXPOSE 9324
