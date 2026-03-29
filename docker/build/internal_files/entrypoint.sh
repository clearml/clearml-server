#!/usr/bin/env bash
set -e

mkdir -p /var/log/clearml

SERVER_TYPE=$1

if (( $# < 1 )) ; then
    echo "The server type was not stated. It should be either apiserver, webserver or fileserver."
    sleep 60
    exit 1

elif [[ ${SERVER_TYPE} == "apiserver" ]]; then
    cd /opt/clearml/
    python3 -m apiserver.apierrors_generator

    if [[ -n $CLEARML_USE_GUNICORN ]]; then
      MAX_REQUESTS=
      if [[ -n $CLEARML_GUNICORN_MAX_REQUESTS ]]; then
        MAX_REQUESTS="--max-requests $CLEARML_GUNICORN_MAX_REQUESTS"
        if [[ -n $CLEARML_GUNICORN_MAX_REQUESTS_JITTER ]]; then
          MAX_REQUESTS="$MAX_REQUESTS --max-requests-jitter $CLEARML_GUNICORN_MAX_REQUESTS_JITTER"
        fi
      fi

      export GUNICORN_CMD_ARGS=${CLEARML_GUNICORN_CMD_ARGS}

      # Note: don't be tempted to "fix" $MAX_REQUESTS with "$MAX_REQUESTS" as this produces an empty arg which fucks up gunicorn
      gunicorn \
        -w "${CLEARML_GUNICORN_WORKERS:-8}" \
        -t "${CLEARML_GUNICORN_TIMEOUT:-600}" --bind="${CLEARML_GUNICORN_BIND:-0.0.0.0:8008}" \
        $MAX_REQUESTS apiserver.server:app
    else
        python3 -m apiserver.server
    fi

elif [[ ${SERVER_TYPE} == "webserver" ]]; then

    if [[ "${USER_KEY}" != "" ]] || [[ "${USER_SECRET}" != "" ]] || [[ "${COMPANY_ID}" != "" ]]; then
      cat << EOF > /usr/share/nginx/html/credentials.json
{
  "userKey": "${USER_KEY}",
  "userSecret": "${USER_SECRET}",
  "companyID": "${COMPANY_ID}"
}
EOF
    fi

    # Create an empty configuration json
    echo "{}" > /tmp/configuration.json
	
    # Copy the external configuration file if it exists
    if test -f "/mnt/external_files/configs/configuration.json"; then
      echo "Copying external configuration"
      cp /mnt/external_files/configs/configuration.json /tmp/configuration.json
    fi

	  # Update from env variables
    echo "Updating configuration from env"
    /opt/clearml/utilities/update_from_env.py \
        --verbose \
        /tmp/configuration.json \
        /usr/share/nginx/html/configuration.json

    export NGINX_APISERVER_ADDR=${NGINX_APISERVER_ADDRESS:-http://apiserver:8008}
    export NGINX_FILESERVER_ADDR=${NGINX_FILESERVER_ADDRESS:-http://fileserver:8081}
    export NGINX_WEBSERVER_PORT=${NGINX_WEBSERVER_PORT:-80}
    export COMMENT_IPV6_LISTEN=$([ "$DISABLE_NGINX_IPV6" = "true" ] && echo "#" || echo "")
    envsubst '${NGINX_WEBSERVER_PORT} ${COMMENT_IPV6_LISTEN} ${NGINX_APISERVER_ADDR} ${NGINX_FILESERVER_ADDR}' < /etc/nginx/clearml.conf.template > /etc/nginx/sites-enabled/default

    if [[ -n "${CLEARML_SERVER_SUB_PATH}" ]]; then
      mkdir -p /etc/nginx/default.d/
      envsubst '${CLEARML_SERVER_SUB_PATH}' < /etc/nginx/clearml_subpath.conf.template > /etc/nginx/default.d/clearml_subpath.conf
      cp /usr/share/nginx/html/env.js /usr/share/nginx/html/env.js.origin
      envsubst '${CLEARML_SERVER_SUB_PATH}' < /usr/share/nginx/html/env.js.origin > /usr/share/nginx/html/env.js
      cp /usr/share/nginx/html/index.html /usr/share/nginx/html/index.html.origin
      sed 's/href="\/"/href="\/'${CLEARML_SERVER_SUB_PATH}'\/"/' /usr/share/nginx/html/index.html.origin > /usr/share/nginx/html/index.html
    fi

    #start the server
    /usr/sbin/nginx -g "daemon off;"

elif [[ ${SERVER_TYPE} == "fileserver" ]]; then
    cd /opt/clearml/fileserver/

    if [[ -n $FILESERVER_USE_GUNICORN ]]; then
      MAX_REQUESTS=
      if [[ -n $FILESERVER_GUNICORN_MAX_REQUESTS ]]; then
        MAX_REQUESTS="--max-requests $FILESERVER_GUNICORN_MAX_REQUESTS"
        if [[ -n $FILESERVER_GUNICORN_MAX_REQUESTS_JITTER ]]; then
          MAX_REQUESTS="$MAX_REQUESTS --max-requests-jitter $FILESERVER_GUNICORN_MAX_REQUESTS_JITTER"
        fi
      fi

      export GUNICORN_CMD_ARGS=${FILESERVER_GUNICORN_CMD_ARGS}

      # Note: don't be tempted to "fix" $MAX_REQUESTS with "$MAX_REQUESTS" as this produces an empty arg which fucks up gunicorn
      gunicorn \
        -w "${FILESERVER_GUNICORN_WORKERS:-8}" \
        -t "${FILESERVER_GUNICORN_TIMEOUT:-600}" --bind="${FILESERVER_GUNICORN_BIND:-0.0.0.0:8081}" \
        $MAX_REQUESTS fileserver:app
    else
        python3 fileserver.py
    fi

else
    echo "Server type ${SERVER_TYPE} is invalid. Please choose either apiserver, webserver or fileserver."
fi
