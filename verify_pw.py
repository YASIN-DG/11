from werkzeug.security import check_password_hash
hash = 'scrypt:32768:8:1$SMdjaZWdFpz4giw4$a0955474c719e209e2b74ba61d64bde37ba02454eb304577d6fef198be59efe84921e6b711dd3eaba8e2ee0480e183a016d52c074cf600cc4d5dc98a1b43bb1d'
print(check_password_hash(hash, 'admin'))
