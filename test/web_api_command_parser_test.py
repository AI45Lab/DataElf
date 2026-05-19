import unittest


from web_api.command_parser import parse_user_command


class WebApiCommandParserTest(unittest.TestCase):
    def test_parse_explicit_elf_run_with_quoted_task(self):
        parsed = parse_user_command('elf run "run security_audit on security_audit_samples"')

        self.assertEqual(parsed.mode, "run")
        self.assertEqual(parsed.task, "run security_audit on security_audit_samples")
        self.assertEqual(parsed.raw_command, 'elf run "run security_audit on security_audit_samples"')

    def test_parse_plain_text_defaults_to_run(self):
        parsed = parse_user_command("audit security_audit_samples with default checkers")

        self.assertEqual(parsed.mode, "run")
        self.assertEqual(parsed.task, "audit security_audit_samples with default checkers")

    def test_parse_non_run_mode_keeps_task_remainder(self):
        parsed = parse_user_command("elf pilot improve audit coverage")

        self.assertEqual(parsed.mode, "pilot")
        self.assertEqual(parsed.task, "improve audit coverage")


if __name__ == "__main__":
    unittest.main()
