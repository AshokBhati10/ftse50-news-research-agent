FROM alpine:3.20
RUN apk add --no-cache tor \
 && printf 'SocksPort 0.0.0.0:9050\nSocksPolicy accept 172.16.0.0/12\nSocksPolicy accept 10.0.0.0/8\nSocksPolicy accept 192.168.0.0/16\nSocksPolicy reject *\nDataDirectory /var/lib/tor\n' > /etc/tor/torrc
EXPOSE 9050
CMD ["tor", "-f", "/etc/tor/torrc"]
