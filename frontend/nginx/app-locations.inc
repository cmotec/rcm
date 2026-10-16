# The application's locations, shared by the HTTPS server and (HTTP_MODE=serve) the HTTP server.

location /api/ {
    proxy_pass http://api_backend;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_request_buffering off;
    proxy_buffering off;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    include /etc/nginx/templates/security-headers.inc;
}

location = /healthz { proxy_pass http://api_backend; access_log off; }
location = /readyz { proxy_pass http://api_backend; access_log off; }

location /assets/ {
    expires 1y;
    add_header Cache-Control "public, immutable";
    include /etc/nginx/templates/security-headers.inc;
    try_files $uri =404;
}

location / {
    add_header Cache-Control "no-cache";
    include /etc/nginx/templates/security-headers.inc;
    try_files $uri /index.html;
}
