FROM acryldata/datahub-actions:v0.0.15

#USER root
COPY ./*.whl .

RUN pip install acryl_datahub-0.13.2-py3-none-any.whl
RUN pip install acryl_datahub-0.13.2-py3-none-any.whl[s3]
RUN pip install acryl_datahub-0.13.2-py3-none-any.whl[mssql]
RUN pip install acryl_datahub-0.13.2-py3-none-any.whl[mariadb]
RUN pip install acryl_datahub-0.13.2-py3-none-any.whl[oracle]
RUN pip install acryl_datahub-0.13.2-py3-none-any.whl[clickhouse]
RUN pip install acryl_datahub-0.13.2-py3-none-any.whl[airflow]
RUN pip install acryl_datahub-0.13.2-py3-none-any.whl[openapi]

RUN pip install --no-cache-dir --upgrade pip && \
    pip install pymysql==1.1.1 && \
    pip install pyarrow==14.0.1 && \
    pip install flask-appbuilder==4.3.11 && \
    pip install acryl-datahub-classify==0.0.11 && \ 
    pip install spacy==3.7.6

RUN pip install en_core_web_sm-3.7.1-py3-none-any.whl

RUN pip uninstall -y pyspark
RUN pip install pyspark-3.4.3-py2.py3-none-any.whl


USER root
# Avoid CVEs
RUN apt-get install libkrb5-dev
RUN apt-get update && \
    apt-get install -y libaom-dev
#RUN apt-get install --only-upgrade update krb5*
#RUN apt-get install --only-upgrade update aom*