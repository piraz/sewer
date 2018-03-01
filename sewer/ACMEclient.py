import time
import copy
import json
import base64
import hashlib
import binascii
import urllib.parse
import textwrap
import platform

import requests
import OpenSSL
import cryptography
from structlog import get_logger

from . import __version__ as sewer_version


class ACMEclient(object):
    """
    todo: improve documentation.

    usage:
        import sewer
        dns_class = sewer.CloudFlareDns(CLOUDFLARE_DNS_ZONE_ID='random',
                                        CLOUDFLARE_EMAIL='example@example.com',
                                        CLOUDFLARE_API_KEY='nsa-grade-api-key')

        1. to create a new certificate.
        client = sewer.Client(domain_name='example.com',
                              dns_class=dns_class)
        certificate = client.cert()
        certificate_key = client.certificate_key
        account_key = client.account_key

        with open('certificate.crt', 'w') as certificate_file:
            certificate_file.write(certificate)

        with open('certificate.key', 'w') as certificate_key_file:
            certificate_key_file.write(certificate_key)


        2. to renew a certificate:
        with open('account_key.key', 'r') as account_key_file:
            account_key = account_key_file.read()

        client = sewer.Client(domain_name='example.com',
                              dns_class=dns_class,
                              account_key=account_key)
        certificate = client.renew()
        certificate_key = client.certificate_key

    todo:
        - handle exceptions
    """

    def __init__(
            self,
            domain_name,
            dns_class,
            domain_alt_names=[],
            registration_recovery_email=None,
            account_key=None,
            bits=2048,
            digest='sha256',
            ACME_REQUEST_TIMEOUT=65,
            ACME_CHALLENGE_WAIT_PERIOD=8,
            ACME_DIRECTORY_URL='https://acme-staging-v02.api.letsencrypt.org/directory',
            ACME_CERTIFICATE_CHAIN_URL='https://letsencrypt.org/certs/fakelerootx1.pem'):

        self.logger = get_logger(__name__).bind(
            client_name=self.__class__.__name__)

        self.domain_name = domain_name
        self.dns_class = dns_class
        self.domain_alt_names = domain_alt_names
        self.all_domain_names = copy.copy(self.domain_alt_names)
        self.all_domain_names.insert(0, self.domain_name)
        self.registration_recovery_email = registration_recovery_email
        self.bits = bits
        self.digest = digest
        self.ACME_REQUEST_TIMEOUT = ACME_REQUEST_TIMEOUT
        self.ACME_CHALLENGE_WAIT_PERIOD = ACME_CHALLENGE_WAIT_PERIOD
        self.ACME_DIRECTORY_URL = ACME_DIRECTORY_URL
        self.ACME_CERTIFICATE_CHAIN_URL = ACME_CERTIFICATE_CHAIN_URL

        self.User_Agent = self.get_user_agent()
        acme_endpoints = self.get_acme_endpoints().json()
        self.ACME_GET_NONCE_URL = acme_endpoints['newNonce']
        self.ACME_TOS_URL = acme_endpoints['meta']['termsOfService']
        self.ACME_KEY_CHANGE_URL = acme_endpoints['keyChange']
        self.ACME_NEW_ACCOUNT_URL = acme_endpoints['newAccount']
        self.ACME_NEW_ORDER_URL = acme_endpoints['newOrder']
        self.ACME_REVOKE_CERT_URL = acme_endpoints['revokeCert']

        # unique account identifier
        # https://tools.ietf.org/html/draft-ietf-acme-acme-09#section-6.2
        self.kid = None

        self.certificate_key = self.create_certificate_key()
        self.csr = self.create_csr()
        self.certificate_chain = self.get_certificate_chain()

        if not account_key:
            self.account_key = self.create_account_key()
            self.PRIOR_REGISTERED = False
        else:
            self.account_key = account_key
            self.PRIOR_REGISTERED = True

        self.logger = self.logger.bind(
            sewer_client_name=self.__class__.__name__,
            sewer_client_version=sewer_version.__version__,
            domain_names=self.all_domain_names,
            ACME_DIRECTORY_URL=self.ACME_DIRECTORY_URL)

        # for staging/test, use:
        # ACME_CERTIFICATE_CHAIN_URL= 'https://letsencrypt.org/certs/fakelerootx1.pem'
        # for prod use:
        # ACME_CERTIFICATE_CHAIN_URL = 'https://letsencrypt.org/certs/lets-encrypt-x3-cross-signed.pem'

    def log_response(self, response):
        """
        renders response as json or as a string
        """
        # TODO: use this to handle all response logs.
        try:
            log_body = response.json()
        except ValueError:
            log_body = response.content
        return log_body

    def get_user_agent(self):
        # TODO: add the sewer-acme versionto the User-Agent
        return "python-requests/{requests_version} ({system}: {machine}) sewer {sewer_version} ({sewer_url})".format(
            requests_version=requests.__version__,
            system=platform.system(),
            machine=platform.machine(),
            sewer_version=sewer_version.__version__,
            sewer_url=sewer_version.__url__)

    def get_acme_endpoints(self):
        self.logger.info('get_acme_endpoints')
        headers = {'User-Agent': self.User_Agent}
        get_acme_endpoints = requests.get(
            self.ACME_DIRECTORY_URL,
            timeout=self.ACME_REQUEST_TIMEOUT,
            headers=headers)
        self.logger.info(
            'get_acme_endpoints_response',
            status_code=get_acme_endpoints.status_code)
        if get_acme_endpoints.status_code not in [200, 201]:
            raise ValueError(
                "Error while getting Acme endpoints: status_code={status_code} response={response}". format(
                    status_code=get_acme_endpoints.status_code,
                    response=self.log_response(get_acme_endpoints)))
        return get_acme_endpoints

    def get_nonce(self):
        """
        https://tools.ietf.org/html/draft-ietf-acme-acme-09#section-6.4
        Each request to an ACME server must include a fresh unused nonce
        in order to protect against replay attacks.
        """
        self.logger.info('get_nonce')
        headers = {'User-Agent': self.User_Agent}
        response = requests.get(
            self.ACME_GET_NONCE_URL,
            timeout=self.ACME_REQUEST_TIMEOUT,
            headers=headers)
        nonce = response.headers['Replay-Nonce']
        return nonce

    def create_account_key(self):
        self.logger.info('create_account_key')
        return self.create_key()

    def create_certificate_key(self):
        self.logger.info('create_certificate_key')
        return self.create_key()

    def create_key(self, key_type=OpenSSL.crypto.TYPE_RSA):
        key = OpenSSL.crypto.PKey()
        key.generate_key(key_type, self.bits)
        private_key = OpenSSL.crypto.dump_privatekey(
            OpenSSL.crypto.FILETYPE_PEM, key)
        return private_key

    def create_csr(self):
        self.logger.info('create_csr')
        X509Req = OpenSSL.crypto.X509Req()
        X509Req.get_subject().CN = self.domain_name

        if self.domain_alt_names:
            SAN = 'DNS:{0}, '.format(self.domain_name).encode('utf8') + \
                  ', '.join('DNS:' + i for i in self.domain_alt_names).encode('utf8')
        else:
            SAN = 'DNS:{0}'.format(self.domain_name).encode('utf8')

        X509Req.add_extensions([
            OpenSSL.crypto.X509Extension(
                'subjectAltName'.encode('utf8'), critical=False, value=SAN)
        ])
        pk = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
                                            self.certificate_key)
        X509Req.set_pubkey(pk)
        X509Req.set_version(2)
        X509Req.sign(pk, self.digest)
        return OpenSSL.crypto.dump_certificate_request(
            OpenSSL.crypto.FILETYPE_ASN1, X509Req)

    def get_certificate_chain(self):
        self.logger.info('get_certificate_chain')
        headers = {'User-Agent': self.User_Agent}
        get_certificate_chain_response = requests.get(
            self.ACME_CERTIFICATE_CHAIN_URL,
            timeout=self.ACME_REQUEST_TIMEOUT,
            headers=headers)
        certificate_chain = get_certificate_chain_response.content
        self.logger.info(
            'get_certificate_chain_response',
            status_code=get_certificate_chain_response.status_code)

        if get_certificate_chain_response.status_code not in [200, 201]:
            raise ValueError(
                "Error while getting Acme certificate chain: status_code={status_code} response={response}". format(
                    status_code=get_certificate_chain_response.status_code,
                    response=self.log_response(get_certificate_chain_response)))
        elif b'-----BEGIN CERTIFICATE-----' and b'-----END CERTIFICATE-----' not in get_certificate_chain_response.content:
            raise ValueError(
                "Error while getting Acme certificate chain: status_code={status_code} response={response}". format(
                    status_code=get_certificate_chain_response.status_code,
                    response=self.log_response(get_certificate_chain_response)))

        return certificate_chain

    def calculate_safe_base64(self, un_encoded_data):
        """
        takes in a string or bytes
        returns a string
        """
        if isinstance(un_encoded_data, str):
            un_encoded_data = un_encoded_data.encode('utf8')
        r = base64.urlsafe_b64encode(un_encoded_data).rstrip(b'=')
        return r.decode('utf8')

    def sign_message(self, message):
        self.logger.info('sign_message')
        pk = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
                                            self.account_key)
        return OpenSSL.crypto.sign(pk, message.encode('utf8'), self.digest)

    def get_acme_header(self, url):
        """
        https://tools.ietf.org/html/draft-ietf-acme-acme-09#section-6.2
        The JWS Protected Header MUST include the following fields:
        - "alg" (Algorithm)
        - "jwk" (JSON Web Key, only for requests to new-account and revoke-cert resources)
        - "kid" (Key ID, for all other requests). gotten from self.ACME_NEW_ACCOUNT_URL
        - "nonce". gotten from self.ACME_GET_NONCE_URL
        - "url"
        """
        self.logger.info('get_acme_header')
        header = {"alg": "RS256", "nonce": self.get_nonce(), "url": url}

        if url in [
                self.ACME_NEW_ACCOUNT_URL,
                self.ACME_REVOKE_CERT_URL,
                "GET_THUMBPRINT"]:
            private_key = cryptography.hazmat.primitives.serialization.load_pem_private_key(
                self.account_key,
                password=None,
                backend=cryptography.hazmat.backends.default_backend())
            public_key_public_numbers = private_key.public_key().public_numbers()
            # private key public exponent in hex format
            exponent = "{0:x}".format(public_key_public_numbers.e)
            exponent = "0{0}".format(exponent) if len(
                exponent) % 2 else exponent
            # private key modulus in hex format
            modulus = "{0:x}".format(public_key_public_numbers.n)
            jwk = {
                "kty": "RSA", "e": self.calculate_safe_base64(
                    binascii.unhexlify(exponent)), "n": self.calculate_safe_base64(
                    binascii.unhexlify(modulus))}
            header["jwk"] = jwk
        else:
            header["kid"] = self.kid

        return header

    def apply_for_cert_issuance(self):
        """
        https://tools.ietf.org/html/draft-ietf-acme-acme-09#section-7.4
        The order object returned by the server represents a promise that if
        the client fulfills the server's requirements before the "expires"
        time, then the server will be willing to finalize the order upon
        request and issue the requested certificate.  In the order object,
        any authorization referenced in the "authorizations" array whose
        status is "pending" represents an authorization transaction that the
        client must complete before the server will issue the certificate.

        Once the client believes it has fulfilled the server's requirements,
        it should send a POST request to the order resource's finalize URL.
        The POST body MUST include a CSR:
        """
        self.logger.info('apply_for_cert_issuance')
        # TODO: factor in self.all_domain_names instead of just
        # self.domain_name
        payload = {
            "identifiers": [{"type": "dns", "value": self.domain_name}],
            # the date values seem to be ignored by LetsEncrypt although they are
            # in the ACME draft spec; https://tools.ietf.org/html/draft-ietf-acme-acme-09#section-7.4
            #    "notBefore": "2016-01-01T00:00:00Z",
            #    "notAfter": "2016-01-08T00:00:00Z"
        }

        url = self.ACME_NEW_ORDER_URL
        apply_for_cert_issuance_response = self.make_signed_acme_request(
            url=url,
            payload=payload)
        self.logger.info(
            'apply_for_cert_issuance_response',
            status_code=apply_for_cert_issuance_response.status_code,
            response=self.log_response(apply_for_cert_issuance_response))

        if apply_for_cert_issuance_response.status_code != 201:
            raise ValueError(
                "Error applying for certificate issuance: status_code={status_code} response={response}". format(
                    status_code=apply_for_cert_issuance_response.status_code,
                    response=self.log_response(apply_for_cert_issuance_response)))

        apply_for_cert_issuance_response_json = apply_for_cert_issuance_response.json()
        # list
        authorizations = apply_for_cert_issuance_response_json["authorizations"]
        authorization_url = authorizations[0]
        finalize = apply_for_cert_issuance_response_json["finalize"]
        certificate_url = apply_for_cert_issuance_response_json["certificate"]

        return authorization_url, finalize_url

    def send_csr(self, finalize_url):
        """
        https://tools.ietf.org/html/draft-ietf-acme-acme-09#section-7.4
        Once the client believes it has fulfilled the server's requirements,
        it should send a POST request(include a CSR) to the order resource's finalize URL.
        A request to finalize an order will result in error if the order indicated does not have status "pending",
        if the CSR and order identifiers differ, or if the account is not authorized for the identifiers indicated in the CSR.

        A valid request to finalize an order will return the order to be finalized.
        The client should begin polling the order by sending a
        GET request to the order resource to obtain its current state.
        """
        self.logger.info('send_csr')
        payload = {"csr": self.csr}
        send_csr_response = self.make_signed_acme_request(
            url=finalize_url, payload=payload)
        self.logger.info(
            'send_csr_response',
            status_code=send_csr_response.status_code,
            response=self.log_response(send_csr_response))

        if send_csr_response.status_code != 200:
            raise ValueError(
                "Error sending csr: status_code={status_code} response={response}". format(
                    status_code=send_csr_response.status_code,
                    response=self.log_response(send_csr_response)))
        return send_csr_response

    def make_signed_acme_request(self, url, payload):
        self.logger.info('make_signed_acme_request')
        headers = {'User-Agent': self.User_Agent}
        if payload in ['GET_CHALLENGE', 'GET_CERTIFICATE']:
            response = requests.get(
                url, timeout=self.ACME_REQUEST_TIMEOUT, headers=headers)
        else:
            payload64 = self.calculate_safe_base64(json.dumps(payload))
            protected = self.get_acme_header(url)
            protected64 = self.calculate_safe_base64(json.dumps(protected))
            signature = self.sign_message(
                message="{0}.{1}".format(
                    protected64, payload64))  # bytes
            signature64 = self.calculate_safe_base64(signature)  # str
            data = json.dumps(
                {"protected": protected64, "payload": payload64,
                 "signature": signature64})
            response = requests.post(
                url,
                data=data.encode('utf8'),
                timeout=self.ACME_REQUEST_TIMEOUT,
                headers=headers)
        return response

    def acme_register(self):
        """
        https://tools.ietf.org/html/draft-ietf-acme-acme-09#section-7.3
        The server creates an account and stores the public key used to
        verify the JWS (i.e., the "jwk" element of the JWS header) to
        authenticate future requests from the account.
        The server returns this account object in a 201 (Created) response, with the account URL
        in a Location header field.
        This account URL will be used in subsequest requests to ACME, as the "kid" value in the acme header.
        """
        self.logger.info('acme_register')
        if self.PRIOR_REGISTERED:
            payload = {"onlyReturnExisting": True}
        elif self.registration_recovery_email:
            payload = {
                "termsOfServiceAgreed": True, "contact": [
                    "mailto:{0}".format(
                        self.registration_recovery_email)]}
        else:
            payload = {"termsOfServiceAgreed": True}

        url = self.ACME_NEW_ACCOUNT_URL
        acme_register_response = self.make_signed_acme_request(
            url=url, payload=payload)
        self.logger.info(
            'acme_register_response',
            status_code=acme_register_response.status_code,
            response=self.log_response(acme_register_response))

        if acme_register_response.status_code not in [201, 409]:
            raise ValueError(
                "Error while registering: status_code={status_code} response={response}". format(
                    status_code=acme_register_response.status_code,
                    response=self.log_response(acme_register_response)))

        kid = acme_register_response.headers['Location']
        setattr(self, 'kid', kid)
        return acme_register_response

    def get_challenge(self, url):
        """
        https://tools.ietf.org/html/draft-ietf-acme-acme-09#section-7.5
        When a client receives an order(ie after self.apply_for_cert_issuance() succeeds)
        from the server it downloads the authorization resources by sending
        GET requests to the indicated URLs.

        If a client wishes to relinquish its authorization to issue
        certificates for an identifier, then it may request that the server
        deactivates each authorization associated with it by sending POST
        requests with the static object {"status": "deactivated"} to each
        authorization URL.
        """
        self.logger.info('get_challenge')
        challenge_response = self.make_signed_acme_request(
            url, payload='GET_CHALLENGE')
        self.logger.info(
            'get_challenge_response',
            status_code=challenge_response.status_code,
            response=self.log_response(challenge_response))

        if challenge_response.status_code != 200:
            raise ValueError(
                "Error requesting for challenges: status_code={status_code} response={response}". format(
                    status_code=challenge_response.status_code,
                    response=self.log_response(challenge_response)))

        challenge_response_json = challenge_response.json()
        for i in challenge_response_json['challenges']:
            if i['type'] == 'dns-01':
                dns_challenge = i
        dns_token = dns_challenge['token']
        dns_challenge_url = dns_challenge['url']
        return dns_token, dns_challenge_url

    def get_keyauthorization(self, dns_token):
        self.logger.info('get_keyauthorization')
        acme_header_jwk_json = json.dumps(
            self.get_acme_header("GET_THUMBPRINT")['jwk'],
            sort_keys=True,
            separators=(',', ':'))
        acme_thumbprint = self.calculate_safe_base64(
            hashlib.sha256(acme_header_jwk_json.encode('utf8')).digest())
        acme_keyauthorization = "{0}.{1}".format(dns_token, acme_thumbprint)
        base64_of_acme_keyauthorization = self.calculate_safe_base64(
            hashlib.sha256(acme_keyauthorization.encode("utf8")).digest())

        return acme_keyauthorization, base64_of_acme_keyauthorization

    def respond_to_challenge(self, acme_keyauthorization, dns_challenge_url):
        """
        https://tools.ietf.org/html/draft-ietf-acme-acme-09#section-7.5.1
        To prove control of the identifier and receive authorization, the
        client needs to respond with information to complete the challenges.
        The server is said to "finalize" the authorization when it has
        completed one of the validations, by assigning the authorization a
        status of "valid" or "invalid".

        Usually, the validation process will take some time, so the client
        will need to poll the authorization resource to see when it is finalized.
        To check on the status of an authorization, the client sends a GET(polling)
        request to the authorization URL, and the server responds with the
        current authorization object.
        """
        self.logger.info('respond_to_challenge')
        payload = {"keyAuthorization": "{0}".format(acme_keyauthorization)}
        notify_acme_challenge_set_response = self.make_signed_acme_request(
            dns_challenge_url,
            payload)
        self.logger.info(
            'respond_to_challenge_response',
            status_code=notify_acme_challenge_set_response.status_code,
            response=self.log_response(notify_acme_challenge_set_response))
        return respond_to_challenge

    def check_authorization_status(
            self,
            authorization_url,
            base64_of_acme_keyauthorization):
        """
        https://tools.ietf.org/html/draft-ietf-acme-acme-09#section-7.5.1
        To check on the status of an authorization, the client sends a GET(polling)
        request to the authorization URL, and the server responds with the
        current authorization object.
        """
        self.logger.info('check_authorization_status')
        time.sleep(self.ACME_CHALLENGE_WAIT_PERIOD)
        number_of_checks = 0
        maximum_number_of_checks_allowed = 5
        while True:
            try:
                headers = {'User-Agent': self.User_Agent}
                check_authorization_status_response = requests.get(
                    authorization_url, timeout=self.ACME_REQUEST_TIMEOUT, headers=headers)
                authorization_status = check_authorization_status_response.json()[
                    'status']
                number_of_checks = number_of_checks + 1
                self.logger.info(
                    'check_authorization_status_response',
                    status_code=check_authorization_status_response.status_code,
                    response=self.log_response(check_authorization_status_response),
                    number_of_checks=number_of_checks)
                if number_of_checks > maximum_number_of_checks_allowed:
                    raise StopIteration(
                        "Number of checks done is {0} which is greater than the maximum allowed of {1}.". format(
                            number_of_checks, maximum_number_of_checks_allowed))
            except Exception as e:
                self.logger.info('check_challenge', error=str(e))
                self.dns_class.delete_dns_record(
                    domain_name, base64_of_acme_keyauthorization)
                break
            if authorization_status == "pending":
                time.sleep(self.ACME_CHALLENGE_WAIT_PERIOD)
            elif authorization_status == "valid":
                self.dns_class.delete_dns_record(
                    domain_name, base64_of_acme_keyauthorization)
                break
            else:
                # for any other status, sleep
                time.sleep(self.ACME_CHALLENGE_WAIT_PERIOD)
        return check_authorization_status_response

    def get_certificate(self, certificate_url):
        self.logger.info('get_certificate')

        get_certificate_response = self.make_signed_acme_request(
            certificate_url, payload='GET_CERTIFICATE')
        self.logger.info(
            'get_certificate_response',
            status_code=get_certificate_response.status_code,
            response=self.log_response(get_certificate_response))

        if get_certificate_response.status_code != 200:
            raise ValueError(
                "Error fetching signed certificate: status_code={status_code} response={response}". format(
                    status_code=get_certificate_response.status_code,
                    response=self.log_response(get_certificate_response)))

        base64encoded_cert = base64.b64encode(
            get_certificate_response.content.encode('utf-8'))
        sixty_four_width_cert = textwrap.wrap(
            base64encoded_cert.decode('utf-8'), 64)
        certificate = '\n'.join(sixty_four_width_cert)

        pem_certificate = """-----BEGIN CERTIFICATE-----\n{0}\n-----END CERTIFICATE-----\n""".format(
            certificate)
        pem_certificate_and_chain = pem_certificate + self.certificate_chain
        return pem_certificate_and_chain

    def just_get_me_a_certificate(self):
        self.logger.info('just_get_me_a_certificate')
        self.acme_register()
        authorization_url, finalize_url, certificate_url = self.apply_for_cert_issuance()
        dns_token, dns_challenge_url = self.get_challenge(
            url=authorization_url)
        acme_keyauthorization, base64_of_acme_keyauthorization = self.get_keyauthorization(
            dns_token)
        self.dns_class.create_dns_record(
            self.domain_name, base64_of_acme_keyauthorization)
        self.send_csr(finalize_url)
        self.respond_to_challenge(acme_keyauthorization, dns_challenge_url)
        self.check_authorization_status(
            authorization_url, base64_of_acme_keyauthorization)
        certificate = self.get_certificate(certificate_url)

        # for domain_name in self.all_domain_names:
        #     # NB: this means we will only get a certificate; self.get_certificate()
        #     # if all the SAN succed the following steps
        #     dns_token, dns_challenge_url = self.get_challenge(domain_name)
        #     acme_keyauthorization, base64_of_acme_keyauthorization = self.get_keyauthorization(
        #         dns_token)
        #     self.dns_class.create_dns_record(domain_name,
        #                                      base64_of_acme_keyauthorization)
        #     self.respond_to_challenge(acme_keyauthorization,  dns_challenge_url)
        #     self.check_authorization_status(
        #         dns_challenge_url,
        #         base64_of_acme_keyauthorization,
        #         domain_name)
        # certificate = self.get_certificate()

        return certificate

    def cert(self):
        """
        convenience method to get a certificate without much hassle
        """
        return self.just_get_me_a_certificate()

    def renew(self):
        """
        renews a certificate.
        A renewal is actually just getting a new certificate.
        An issuance request counts as a renewal if it contains the exact same set of hostnames as a previously issued certificate.
            https://letsencrypt.org/docs/rate-limits/
        """
        return self.just_get_me_a_certificate()
