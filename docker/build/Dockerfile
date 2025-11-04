FROM node:20-bookworm-slim as webapp_builder

ARG CLEARML_WEB_GIT_URL=https://github.com/allegroai/clearml-web.git

USER root
WORKDIR /opt

RUN apt-get update && apt-get install -y git
RUN git clone ${CLEARML_WEB_GIT_URL} clearml-web
RUN mv clearml-web /opt/open-webapp
COPY --chmod=744 docker/build/internal_files/build_webapp.sh /tmp/internal_files/
RUN /bin/bash -c '/tmp/internal_files/build_webapp.sh'

FROM python:3.11-slim-bookworm
COPY --chmod=744 docker/build/internal_files/entrypoint.sh /opt/clearml/
COPY --chmod=744 docker/build/internal_files/update_from_env.py /opt/clearml/utilities/
COPY fileserver /opt/clearml/fileserver/
COPY apiserver /opt/clearml/apiserver/

COPY --chmod=744 docker/build/internal_files/final_image_preparation.sh /tmp/internal_files/
COPY docker/build/internal_files/clearml.conf.template /tmp/internal_files/
COPY docker/build/internal_files/clearml_subpath.conf.template /tmp/internal_files/
RUN /bin/bash -c '/tmp/internal_files/final_image_preparation.sh'

COPY --from=webapp_builder /opt/open-webapp/build /usr/share/nginx/html
COPY --from=webapp_builder /opt/open-webapp/dist/report-widgets /usr/share/nginx/widgets

EXPOSE 8080
EXPOSE 8008
EXPOSE 8081

ARG VERSION
ARG BUILD
ENV CLEARML_SERVER_VERSION=${VERSION}
ENV CLEARML_SERVER_BUILD=${BUILD}

WORKDIR /opt/clearml/
ENTRYPOINT ["/opt/clearml/entrypoint.sh"]
