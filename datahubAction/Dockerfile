FROM acryldata/datahub-actions:v0.0.15

#USER root
COPY ./*.whl .

RUN pip install acryl_datahub-0.12.1.5-py3-none-any.whl[s3] 

FROM python:3
#USER datahub
RUN pip install --no-cache-dir --upgrade pip && \
    pip install pymysql==1.1.1 && \
    pip install pyarrow==14.0.1 && \
    pip install flask-appbuilder==4.3.11