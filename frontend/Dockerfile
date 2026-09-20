# Frontend: Vite build, then nginx serving the static output.
#
# VITE_API_URL is baked in at BUILD time - Vite inlines import.meta.env into
# the bundle. The default is right here, because the browser runs on the host
# and reaches the backend through its published port; it is an ARG so a real
# deployment can override it.

FROM node:22-alpine AS build

WORKDIR /app

# npm ci, not npm install: it installs exactly what the lockfile says and fails
# if the two disagree.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

ARG VITE_API_URL
ENV VITE_API_URL=${VITE_API_URL}
RUN npm run build

FROM nginx:alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
