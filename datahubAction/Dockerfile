FROM acryldata/datahub-actions:v0.0.8

USER root
# Avoid CVEs
#RUN apt-get update && apt-get install libtasn1-6 -y 

RUN apt-get update && apt-get install libksba-dev libapr1 libaprutil1 libtasn1-6 -y 
COPY ./go1.20.3.linux-amd64.tar.gz /usr/local/.
RUN  rm -rf /usr/local/go && tar -C /usr/local -xzf /usr/local/go1.20.3.linux-amd64.tar.gz
# libaprutil1
#libksba apr-util go libstasn1-6

FROM python:3
#USER datahub
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir apache-airflow pyspark
