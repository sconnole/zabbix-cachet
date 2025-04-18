FROM  python:3.11-alpine
MAINTAINER Artem Alexandrov <qk4l@tem4uk.ru>
ENV REFRESHED_AT=2017013001
ENV CONFIG_FILE=/config.yml
COPY requirements.txt /zabbix-cachet/requirements.txt
RUN pip3 install -r /zabbix-cachet/requirements.txt
COPY zabbix-cachet-v3.py /zabbix-cachet/zabbix-cachet-v3.py
WORKDIR /opt/

CMD ["python", "/zabbix-cachet/zabbix-cachet-v3.py"]
