Anonymized samples of mails in the bounce mailbox, as the usual mail servers
send them (helios.tests_bounces classifies each of them). Addresses are
example.org / example.net, IP addresses from 192.0.2.0/24.

{{token}} stands for the VERP token (helios.verp) of the email a test sends;
{{message_id}} for its Message-ID. Our emails ask for RET=HDRS, so a DSN
returns only the headers (text/rfc822-headers), never the body.
