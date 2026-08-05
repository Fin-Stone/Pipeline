# The client, built once and served as static files.
#
# No API address is baked in at any stage. The user enters their server at
# sign-in — their own install or a hosted one — which is what lets the same
# image serve either. See architecture §5.2.

FROM node:22-alpine AS build
WORKDIR /build

# Dependencies first, so a source change does not re-resolve the tree.
COPY ui/pwa/package.json ui/pwa/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY ui/pwa/ ./
RUN npm run build


FROM nginx:1.27-alpine
COPY infra/compose/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html

# nginx's own healthcheck target; the app is static so serving index.html at
# all is the whole of "healthy".
HEALTHCHECK --interval=30s --timeout=3s \
    CMD wget -qO- http://localhost/ >/dev/null || exit 1
