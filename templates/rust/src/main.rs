fn main() {
    println!("{}", greeting());
}

fn greeting() -> &'static str {
    "{{name}}"
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn greets() {
        assert_eq!(greeting(), "{{name}}");
    }
}
